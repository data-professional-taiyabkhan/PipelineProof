"""Evaluate one submission against one task.

    report = evaluate(task, submission_dir, runner, workdir)

Mechanism A: declared seed twice (determinism), two other seeds (sensitivity), the claim
             checked against the clean re-run, declared vs imported packages.
Mechanism B: holdout scored outside the sandbox, trace analysis for leakage, and ablation
             re-runs that neutralise a suspect input column (no edits to candidate code).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis, contract, scoring, static_rules
from .analysis import finding
from .runners.base import EnvSpec, RunSpec
from .runners.docker_runner import EnvBuildError

EXTRA_SEEDS = (101, 202)
PREFLIGHT_ROWS = 400
PREFLIGHT_TIMEOUT_S = 180


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _own_metric(task, out_dir: Path, events):
    m = out_dir / "model" / "metrics.json"
    if m.exists():
        try:
            return float(json.loads(m.read_text())[task.metric])
        except Exception:
            pass
    calls = [e for e in events if e["kind"] == "metric" and e["metric"] == task.metric_fn_name and e.get("depth", 0) == 0]
    return calls[-1]["value"] if calls else None


def _train(task, sub, runner, env_id, data_dir: Path, seed: int, out_dir: Path, timeout=None):
    spec = RunSpec(env=None, submission=sub.root, inputs={"/data": data_dir},
                   command=sub.train_cmd, out_subdirs=["model"],
                   env_vars={"PP_SEED": str(seed), "PP_DATA_DIR": "/data", "PP_OUTPUT_DIR": "/out/model"},
                   timeout_s=timeout or task.time_cap_s, cpus=task.cpus, memory_mb=task.memory_mb)
    return runner.run(spec, env_id, out_dir)


def _predict(task, sub, runner, env_id, model_dir: Path, features_dir: Path, seed: int, out_dir: Path, timeout=None):
    spec = RunSpec(env=None, submission=sub.root, inputs={"/model": model_dir, "/input": features_dir},
                   command=sub.predict_cmd,
                   env_vars={"PP_SEED": str(seed), "PP_MODEL_DIR": "/model",
                             "PP_INPUT": "/input/holdout_features.csv", "PP_PRED_OUT": "/out/predictions.csv",
                             "PP_EVENTS": "/out/predict_events.jsonl"},
                   timeout_s=timeout or task.time_cap_s, cpus=task.cpus, memory_mb=task.memory_mb)
    return runner.run(spec, env_id, out_dir)


def _read_predictions(task, pred_path: Path, ids: pd.Series):
    if not pred_path.exists():
        return None, "predict step wrote no file at PP_PRED_OUT"
    try:
        p = pd.read_csv(pred_path)
    except Exception as e:
        return None, f"predictions file is not a readable CSV: {e}"
    if task.id_column not in p.columns or len(p.columns) != 2:
        return None, f"predictions must have exactly two columns: {task.id_column} and one prediction column"
    col = [c for c in p.columns if c != task.id_column][0]
    p = p.drop_duplicates(task.id_column).set_index(task.id_column)[col]
    missing = (~ids.isin(p.index)).sum()
    if missing:
        return None, f"predictions missing for {missing} rows"
    vals = pd.to_numeric(p.reindex(ids), errors="coerce")
    if vals.isna().any():
        return None, "predictions contain non-numeric values"
    return vals.to_numpy(), None


def run_once(task, sub, runner, env_id, data_dir, seed, out_dir: Path, labels: pd.DataFrame):
    rec = {"seed": seed, "data": data_dir.name}
    tr = _train(task, sub, runner, env_id, data_dir, seed, out_dir / "train")
    events = _read_events(out_dir / "train" / "events.jsonl")
    rec.update(exit=tr.exit_code, wall_s=tr.wall_s)
    if tr.exit_code != 0:
        rec["failure_code"], rec["failure"] = analysis.classify_failure(tr.stderr, tr.exit_code, tr.timed_out)
        rec["stderr_tail"] = tr.stderr[-1500:]
        return rec, events
    rec["own_metric"] = _own_metric(task, out_dir / "train", events)
    pr = _predict(task, sub, runner, env_id, out_dir / "train" / "model", task.holdout_features_dir, seed,
                  out_dir / "predict", )
    rec["predict_wall_s"] = pr.wall_s
    if pr.exit_code != 0:
        rec["predict_failure_code"], rec["predict_failure"] = analysis.classify_failure(pr.stderr, pr.exit_code, pr.timed_out)
        rec["stderr_tail"] = pr.stderr[-1500:]
        return rec, events
    preds, err = _read_predictions(task, out_dir / "predict" / "predictions.csv", labels[task.id_column])
    if err:
        rec["predict_failure_code"], rec["predict_failure"] = "PREDICT_FORMAT", err
        return rec, events
    rec["holdout_metric"] = task.score(labels[task.target].to_numpy(), preds)
    return rec, events


def _ablated_data(task, column, value, work: Path) -> Path:
    d = work / f"data_ablate_{column}"
    d.mkdir(parents=True, exist_ok=True)
    df = task.train_df()
    df[column] = np.random.default_rng(0).permutation(df[column].to_numpy()) if value == "permute" else value
    df.to_csv(d / "train.csv", index=False)
    return d


def evaluate(task, submission_dir, runner, workdir, progress=None) -> dict:
    """progress(stage, message) is called as the evaluation moves through its steps."""
    say = progress or (lambda stage, msg: None)
    t0 = time.time()
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    sub = contract.load(submission_dir, task.metric)
    report = {"task": task.name, "metric": task.metric, "submission": Path(submission_dir).name,
              "runner": runner.name, "contract": {"problems": sub.problems, "warnings": sub.warnings},
              "claimed_metric": sub.claimed_metric, "declared_seed": sub.seed, "findings": [], "runs": []}
    F = report["findings"]
    say("contract", "Checking the submission format (pipeline.toml, requirements.txt, DECISIONS.md)")
    if not sub.ok:
        report["status"] = "contract_failed"
        F += [finding("CONTRACT", "reproducibility", "critical", "contract", p) for p in sub.problems]
        return scoring.finalise(task, report)

    # ---- static pass and declared environment
    say("static", "Reading the code without running it (static rules, imports vs requirements)")
    st = static_rules.analyse(sub.root, task.target, tuple(task.post_outcome_columns))
    report["static"] = st
    undeclared = static_rules.undeclared_imports(st["imports"], sub.requirement_names())
    report["undeclared_imports"] = undeclared
    for mod in undeclared:
        F.append(finding("REPRO_UNDECLARED_IMPORT", "reproducibility", "medium", "static + requirements",
                         f"imports '{mod}' but requirements.txt does not declare it"))

    say("env", "Building the pinned environment")
    try:
        env_id = runner.build_env(EnvSpec(sub.python, [l for l in (sub.root / "requirements.txt").read_text().splitlines()
                                                        if l.strip() and not l.strip().startswith("#")]))
    except EnvBuildError as e:
        report["status"] = "env_failed"
        F.append(finding("ENV_BUILD", "reproducibility", "critical", "environment build",
                         f"the declared requirements could not be installed: {str(e)[-300:]}"))
        return scoring.finalise(task, report)

    labels = pd.read_csv(task.labels_path)
    train_df = task.train_df()

    # ---- Mechanism A
    say("run1", f"Run 1: training with the declared seed ({sub.seed}) and predicting the holdout")
    r1, ev1 = run_once(task, sub, runner, env_id, task.data_dir, sub.seed, workdir / f"seed_{sub.seed}", labels)
    say("run2", f"Run 2: same seed again, to check the result is deterministic")
    r1b, _ = run_once(task, sub, runner, env_id, task.data_dir, sub.seed, workdir / f"seed_{sub.seed}_repeat", labels)
    others = []
    for s in EXTRA_SEEDS:
        say("seeds", f"Run with seed {s}, to measure how much the result depends on the seed")
        others.append(run_once(task, sub, runner, env_id, task.data_dir, s, workdir / f"seed_{s}", labels)[0])
    report["runs"] = [r1, r1b] + others
    ok = r1.get("own_metric") is not None and r1.get("holdout_metric") is not None
    report["completed"] = ok
    report["reproduced_metric"] = r1.get("own_metric")
    report["holdout_metric"] = r1.get("holdout_metric")
    F += analysis.sandbox_findings(ev1)
    if not ok:
        report["status"] = "run_failed"
        code = r1.get("failure_code") or r1.get("predict_failure_code") or "RUN_ERROR"
        F.append(finding(code, "reproducibility", "critical", "re-execution",
                         r1.get("failure") or r1.get("predict_failure") or "the pipeline produced no metric"))
        return scoring.finalise(task, report, runtime_s=time.time() - t0)

    if not any(e["kind"] == "instrumentation_ready" for e in ev1):
        F.append(finding("TELEMETRY_MISSING", "reproducibility", "high", "runtime trace",
                         "the run produced no trace; the pipeline may have disabled the harness's monitoring"))
    claim = sub.claimed_metric
    if claim is not None and abs(claim - r1["own_metric"]) > task.tolerance:
        F.append(finding("REPRO_CLAIM_MISMATCH", "reproducibility", "high", "re-execution",
                         f"claimed {task.metric} {claim:.3f}, but a clean re-run with the declared seed gives "
                         f"{r1['own_metric']:.3f} (tolerance {task.tolerance})"))
    if r1b.get("own_metric") is not None:
        same = abs(r1["own_metric"] - r1b["own_metric"]) < 1e-9 and \
            abs(r1["holdout_metric"] - (r1b.get("holdout_metric") or 0)) < 1e-9
        report["deterministic"] = same
        if not same:
            F.append(finding("REPRO_NONDETERMINISTIC", "reproducibility", "medium", "re-execution",
                             f"two runs with the same seed disagree ({r1['own_metric']:.4f} vs {r1b['own_metric']:.4f}); "
                             f"the pipeline has unseeded randomness"))
    own = [r["own_metric"] for r in [r1] + others if r.get("own_metric") is not None]
    hold = [r["holdout_metric"] for r in [r1] + others if r.get("holdout_metric") is not None]
    report["own_metric_sd"] = float(np.std(own)) if len(own) > 1 else None
    report["holdout_sd"] = float(np.std(hold)) if len(hold) > 1 else None
    if report["own_metric_sd"] == 0 and not st["info"]["uses_pp_seed"]:
        F.append(finding("REPRO_SEED_IGNORED", "reproducibility", "info", "re-execution",
                         "the pipeline ignores PP_SEED, so sensitivity to the seed could not be measured"))
    elif report["own_metric_sd"] and report["own_metric_sd"] > 0.015:
        F.append(finding("REPRO_SEED_SENSITIVE", "reproducibility", "low", "re-execution",
                         f"own metric varies across seeds (sd {report['own_metric_sd']:.3f})"))

    # ---- Mechanism B: gap, trace, ablation
    basis = claim if claim is not None else r1["own_metric"]
    report["gap"] = round((basis - r1["holdout_metric"]) * (1 if task.higher_is_better else -1), 4)
    say("trace", "Analysing the trace: splits, fits, metric calls")
    tf, info = analysis.trace_findings(task, ev1, train_df, claim)
    F += tf
    report["trace"] = info
    report["ablations"] = []
    for col, val in analysis.suspect_columns(task, train_df, set(info["used_columns"]), info["columns_unknown"]).items():
        say("ablation", f"Ablation: re-running with '{col}' neutralised to see if the result depended on it")
        d = _ablated_data(task, col, val, workdir)
        ra, _ = run_once(task, sub, runner, env_id, d, sub.seed, workdir / f"ablate_{col}", labels)
        rec = {"column": col, "set_to": val, "own_before": r1["own_metric"], "own_after": ra.get("own_metric"),
               "holdout_before": r1["holdout_metric"], "holdout_after": ra.get("holdout_metric")}
        report["ablations"].append(rec)
        if ra.get("own_metric") is None:
            continue
        drop = (r1["own_metric"] - ra["own_metric"]) * (1 if task.higher_is_better else -1)
        rec["own_drop"] = round(drop, 4)
        if drop >= 0.03:
            post = col in task.post_outcome_columns
            kind = ", a field not available at prediction time" if post else ", a suspiciously predictive field"
            hold = (f"; holdout {r1['holdout_metric']:.3f} -> {ra['holdout_metric']:.3f}"
                    if ra.get("holdout_metric") is not None else "")
            F.append(finding("LEAK_ABLATION", "leakage", "critical" if post else "high", "ablation re-run",
                             f"the headline result depends on '{col}'{kind}: neutralising it moves the candidate's "
                             f"own {task.metric} from {r1['own_metric']:.3f} to {ra['own_metric']:.3f}{hold}",
                             column=col))

    for s in st["findings"]:
        F.append(finding(s["rule"], "leakage" if s["rule"][:2] in ("S1", "S2", "S3") else
                         ("validation" if s["rule"].startswith("S10") else "reproducibility"),
                         "suspect", "static", f"{s['file']} line {s['line']}: {s['detail']}"))
    report["data_probes"] = static_rules.data_probes(sub.root, task)
    report["validation_info"] = {
        "group_or_time_split": st["info"]["group_or_time_splitter"] or any(s.get("groups") for s in info["splits"]),
        "cross_validation": any(s["func"] != "train_test_split" for s in info["splits"]),
    }
    report["docs"] = {"states_claim": claim is not None,
                      "states_ai_use": "ai" in sub.decisions_text.lower() and "tool" in sub.decisions_text.lower(),
                      "readme": (sub.root / "README.md").exists(), "functions": st["info"]["functions"]}
    report["status"] = "scored"
    say("score", "Scoring and writing the report")
    return scoring.finalise(task, report, runtime_s=time.time() - t0)


def preflight(task, submission_dir, runner, workdir) -> dict:
    """Fast contract check for the upload page: runs train and predict on a small sample.

    Never uses holdout data and never reveals a metric; returns pass/fail messages only."""
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    sub = contract.load(submission_dir, task.metric)
    res = {"ok": False, "problems": list(sub.problems), "warnings": list(sub.warnings)}
    if not sub.ok:
        return res
    df = task.train_df()
    sample = df.sample(n=min(PREFLIGHT_ROWS, len(df)), random_state=0)
    (workdir / "data").mkdir()
    sample.to_csv(workdir / "data" / "train.csv", index=False)
    feats = workdir / "features"
    feats.mkdir()
    probe = sample.drop(columns=[task.target]).head(50)
    probe.to_csv(feats / "holdout_features.csv", index=False)
    try:
        env_id = runner.build_env(EnvSpec(sub.python, [l for l in (sub.root / "requirements.txt").read_text().splitlines()
                                                        if l.strip() and not l.strip().startswith("#")]))
    except EnvBuildError as e:
        res["problems"].append(f"requirements could not be installed: {str(e)[-300:]}")
        return res
    tr = _train(task, sub, runner, env_id, workdir / "data", sub.seed, workdir / "train", timeout=PREFLIGHT_TIMEOUT_S)
    if tr.exit_code != 0:
        res["problems"].append("train step failed: " + analysis.classify_failure(tr.stderr, tr.exit_code, tr.timed_out)[1])
        return res
    if not (workdir / "train" / "model" / "metrics.json").exists():
        res["problems"].append(f"train step did not write metrics.json with a '{task.metric}' value to PP_OUTPUT_DIR")
    pr = _predict(task, sub, runner, env_id, workdir / "train" / "model", feats, sub.seed, workdir / "predict",
                  timeout=PREFLIGHT_TIMEOUT_S)
    if pr.exit_code != 0:
        res["problems"].append("predict step failed: " + analysis.classify_failure(pr.stderr, pr.exit_code, pr.timed_out)[1])
        return res
    _, err = _read_predictions(task, workdir / "predict" / "predictions.csv", probe[task.id_column])
    if err:
        res["problems"].append(err)
    res["ok"] = not res["problems"]
    return res
