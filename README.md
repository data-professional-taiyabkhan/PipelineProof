# PipelineProof

PipelineProof re-runs a candidate's machine-learning take-home in a sealed sandbox, checks the
score they claim against data they never saw, and shows where the work leaks. A named reviewer
confirms or overrides every recommendation.

**Live:** https://data-professional-taiyabkhan--pipelineproof.eu-west.modal.run (assessor demo at `/demo`, no sign-up). Deployment notes: `docs/DEPLOY.md`.

## What is in this repository

```
pipelineproof/        the evaluation harness (Python package)
  contract.py           submission format and validation
  task.py               task definition, metrics, holdout handling
  static_rules.py       code reading: AST rules, notebooks, imports vs requirements
  instrument/           sitecustomize.py injected into every run (runtime trace)
  runners/              local (no isolation), docker, modal (production sandboxes)
  pipeline.py           evaluate() and preflight()
  analysis.py           findings from runs and traces
  scoring.py            rubric v1: six dimensions, published weights
  benchmark.py          planted-defect benchmark and its pass rule
webapp/               the web application (FastAPI, server-rendered pages, HTMX)
  routes/               public site, sign-in, employer workspace, candidate portal
  jobs.py               dispatch and the evaluation worker
  seed.py               assessor demo workspace and benchmark recording
  templates/, static/   pages and the design system
modal_app.py          deployment on Modal: web app, worker, daily maintenance, seed, benchmark
supabase/migrations/  database schema (Postgres, private "pp" schema)
tasks/churn_v1/       Task 1: data with planted traps, holdout, brief, employer notes, starter template
benchmark/            18 planted-defect submissions, run_benchmark.py, logs/
tests/                unit tests and end-to-end web app tests
docs/                 architecture, deployment, runbook, assessor walkthrough
```

## How a submission is evaluated

1. **Contract check**: `pipeline.toml`, pinned `requirements.txt`, `DECISIONS.md` with
   `claimed_metric: <metric> = <value>` and `seed:`.
2. **Pre-flight** (on upload): train and predict on a 400-row sample; never touches the holdout.
3. **Environment build**: the candidate's pinned packages, network on, no data.
4. **Four runs** in fresh sandboxes with the internet blocked: declared seed twice, seeds 101 and 202.
5. **Holdout**: each model predicts unseen rows; labels stay outside the sandbox and score the predictions.
6. **Trace and ablation**: splits, fits and metric calls are recorded; suspect columns are set to their
   prediction-time value and the pipeline re-run.
7. **Report**: six scores with evidence and a recommendation; a reviewer decides, feedback can be
   released, the candidate can contest.

## Run it locally

Needs Python 3.11+ and a Postgres database (local, or the Supabase one).

```
python -m venv .venv && .venv/Scripts/activate          # Windows; use source .venv/bin/activate elsewhere
pip install -r requirements-dev.txt
psql "$DATABASE_URL" -f supabase/migrations/0001_pipelineproof_init.sql   # once, on an empty database
set DATABASE_URL=postgresql://...                        # or put it in your shell profile
uvicorn webapp.main:app --reload                         # http://127.0.0.1:8000
python -m webapp.seed                                    # build the assessor demo workspace
```

Locally the **local runner** executes candidate code as ordinary processes with **no isolation**.
Use it only with the benchmark submissions or code you trust. Production uses Modal sandboxes.

## Tests

```
python -m pytest tests/test_units.py                      # harness unit tests, seconds
python -m pytest tests/test_webapp.py                     # full web flows against Postgres, about 1 minute
python benchmark/run_benchmark.py --runner local          # the 18-submission acceptance test
```

## Deploy

`docs/DEPLOY.md`: Supabase (London) for the database, Modal for the web app, the worker and the
sandboxes. One secret (`DATABASE_URL`) is stored in Modal; nothing secret is in this repository.

## Scope today

- Tabular tasks on CPU, Python 3.11 to 3.13; scripts and notebooks.
- Runtime tracing for scikit-learn, and XGBoost and LightGBM through their scikit-learn interfaces.
- One task (broadband churn). Scoring rubric v1 with hand-set weights.
- The benchmark submissions were written by the build team: they show each check works, not how
  accurate it is on real candidates' code.
- Not yet tested: whether the task's traps still separate careful work from AI-written pipelines.
