# PipelineProof evaluation harness

Phase 1, work packages 1 to 4: the engine that re-runs a candidate's ML take-home in a
sandbox, checks the claimed score against a holdout the candidate never saw, and locates
leakage. The web app (upload, reports, reviewer sign-off) sits on top of this and is next.

## What it does with a submission

1. **Contract check** (`contract.py`): `pipeline.toml`, pinned `requirements.txt`, `DECISIONS.md`
   with `claimed_metric: <metric> = <value>` and `seed:`. Problems block scoring; warnings don't.
2. **Static pass** (`static_rules.py`): AST rules over `.py` files and notebooks, plus imports
   checked against `requirements.txt`.
3. **Environment build** (runner): pinned packages installed with network on and no data.
4. **Mechanism A**: train with the declared seed, again with the same seed, then two other seeds.
   Claim vs clean re-run (task tolerance), determinism, seed spread.
5. **Holdout**: each trained model predicts on holdout features; labels stay in the worker.
6. **Mechanism B** (`analysis.py`): runtime trace (`instrument/sitecustomize.py`) for
   preprocessing before the split, test rows reaching a fit, the test set scored repeatedly,
   entity overlap with a seen-vs-unseen decomposition and bootstrap interval, then ablation
   re-runs that neutralise suspect columns in the input (no edits to candidate code).
7. **Scoring** (`scoring.py`): six dimensions, published weights and deductions per finding
   code (rubric v1). The recommendation is advice; a named reviewer must confirm or override.

## Layout

```
pipelineproof/
  contract.py        submission format and validation
  task.py            task definition, metrics, holdout handling
  static_rules.py    AST rules, notebook reader, import check, task probes
  instrument/        sitecustomize.py injected into every run (tracing, not security)
  runners/           base interface, docker_runner.py (local), modal_runner.py (production)
  analysis.py        findings from runs and traces
  pipeline.py        evaluate() and preflight()
  scoring.py         rubric v1
  report.py          Markdown report and plain-English summary
  cli.py
tasks/churn_v1/      Task 1: data with planted traps, holdout, brief, data dictionary
benchmark/           16 planted-defect submissions, run_benchmark.py, logs/
tests/               unit tests (no Docker)
```

## Running

```
pip install -e ".[dev]"
python -m pytest tests                                    # unit tests
python benchmark/run_benchmark.py                         # end-to-end acceptance test (Docker)
python -m pipelineproof preflight tasks/churn_v1 path/to/submission
python -m pipelineproof evaluate  tasks/churn_v1 path/to/submission --out out/
```

Runner selection: `--runner docker` (default) or `--runner modal`.

- Docker builds `python:<ver>-slim` + the candidate's requirements per submission, cached by
  a hash. Set `PP_DOCKER_RUNTIME=runsc` on a host with gVisor installed.
  `PP_DOCKER_HOST_PYTHON=1` mounts the host's Python instead; it exists only for build
  workspaces that cannot pull base images.
- Modal needs `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`; runs use gVisor, `block_network=True`
  and `region="uk"` (override with `PP_MODAL_REGION`). Written against modal 1.6.2's API but
  **not yet run against a live account**.

## The submission contract (what candidates get in their template)

```
pipeline.toml     python = "3.12"
                  train = "python train.py"            # or: papermill train.ipynb /tmp/out.ipynb
                  predict = "python predict.py"
requirements.txt  name==version, one per line; no URLs or index options
DECISIONS.md      claimed_metric: roc_auc = 0.83
                  seed: 42
                  how you validated; which AI tools you used
```

Train reads `PP_DATA_DIR/train.csv` and `PP_SEED`, writes the model and
`metrics.json` (`{"roc_auc": 0.83}`) to `PP_OUTPUT_DIR`. Predict reads the model from
`PP_MODEL_DIR` and features from `PP_INPUT`, and writes a CSV with two columns, the task's
id column and a prediction, to `PP_PRED_OUT`. No network at run time.

## Known limits (honest list)

- Tracing covers scikit-learn and the scikit-learn APIs of XGBoost and LightGBM. Native
  `xgb.train` / `lgb.train` calls, PyTorch and custom loops are not traced; the holdout gap
  and ablations still apply to them.
- Row matching needs pandas indexes or matching representations; arrays and `reset_index`
  blind parts of the trace.
- Tracing is telemetry. Candidate code can delete it (detected as `TELEMETRY_MISSING`) but
  could also forge it; the sandbox and outside-the-sandbox holdout scoring are the trust anchors.
- Data-handling probes are regexes over code: evidence for the reviewer, not proof.
- Rubric weights and deductions are hand-set (v1) and published in every report.
- The benchmark is written by us; it proves the checks work, not their accuracy on real candidates.
