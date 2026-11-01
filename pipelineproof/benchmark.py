"""Planted-defect benchmark: the acceptance test for the harness.

Each benchmark submission has one known property. A submission PASSES when every expected
finding code is present and no unexpected blocking finding (critical/high/medium) appears.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

EXPECTED = {   # submission -> finding codes that must appear (empty = clean, nothing blocking may appear)
    "p01_clean": set(),
    "p02_target_leak": {"LEAK_ABLATION"},
    "p03_random_split": {"LEAK_ENTITY"},
    "p04_scaler_before_split": {"LEAK_PREPROCESS_BEFORE_SPLIT"},
    "p05_unseeded": {"REPRO_NONDETERMINISTIC"},
    "p06_local_file": {"RUN_MISSING_FILE", "SANDBOX_OUTSIDE_FILE"},
    "p07_undeclared_dep": {"REPRO_UNDECLARED_IMPORT"},
    "p08_inflated_claim": {"REPRO_CLAIM_MISMATCH"},
    "p09_tune_on_test": {"VAL_TEST_REUSED"},
    "p10_network": {"RUN_NETWORK", "SANDBOX_NETWORK"},
    "p11_test_in_fit": {"LEAK_TEST_IN_FIT"},
    "p12_notebook_clean": set(),
    "p13_xgboost_leak": {"LEAK_ABLATION"},
    "p14_lightgbm_clean": set(),
    "p15_deletes_trace": {"TELEMETRY_MISSING"},
    "p16_notebook_scaler_first": {"LEAK_PREPROCESS_BEFORE_SPLIT"},
    "p17_renamed_leak": {"LEAK_ABLATION"},
    "p18_groupkfold_clean": set(),
}
PLANTED = {
    "p01_clean": "None: a careful pipeline",
    "p02_target_leak": "Uses a field set after the customer asks to cancel",
    "p03_random_split": "Random row split, so the same customers are in training and test",
    "p04_scaler_before_split": "Imputer and scaler fitted on all rows before the split",
    "p05_unseeded": "Unseeded randomness",
    "p06_local_file": "Reads a file from the author's laptop that was never submitted",
    "p07_undeclared_dep": "Imports a package that requirements.txt does not list",
    "p08_inflated_claim": "Claims a score the code does not produce",
    "p09_tune_on_test": "Chooses the model by scoring the test set repeatedly",
    "p10_network": "Downloads data from an external API during training",
    "p11_test_in_fit": "Fits the imputer on rows that include the test set",
    "p12_notebook_clean": "None: a careful pipeline written as a notebook",
    "p13_xgboost_leak": "Uses the post-cancellation field, with XGBoost",
    "p14_lightgbm_clean": "None: a careful pipeline using LightGBM",
    "p15_deletes_trace": "Deletes the harness's trace file to hide what it did",
    "p16_notebook_scaler_first": "Notebook that fits the scaler before the split",
    "p17_renamed_leak": "Uses the post-cancellation field under another name (added 1 Nov 2026: the first version missed this)",
    "p18_groupkfold_clean": "None: a careful pipeline validated with a hand-written GroupKFold loop (added 1 Nov 2026: the first version wrongly flagged this)",
}
BLOCKING = {"critical", "high", "medium"}
# findings that legitimately accompany an expected one
COMPANIONS = {"LEAK_ABLATION": {"REPRO_CLAIM_MISMATCH"},
              "REPRO_NONDETERMINISTIC": {"REPRO_CLAIM_MISMATCH"}}   # an unseeded claim cannot reproduce


def judge(name: str, r: dict) -> tuple[bool, list[str], list[str]]:
    codes = {f["code"] for f in r["findings"]}
    blocking = {f["code"] for f in r["findings"] if f["severity"] in BLOCKING}
    want = EXPECTED[name]
    allowed = set(want) | set().union(*(COMPANIONS.get(c, set()) for c in want)) if want else set()
    missing = want - codes
    unexpected = blocking - allowed
    return not missing and not unexpected, sorted(missing), sorted(unexpected)


def row(name: str, r: dict, seconds: float) -> dict:
    ok, missing, unexpected = judge(name, r)
    return {"submission": name, "planted": PLANTED.get(name, ""), "pass": ok, "expected": sorted(EXPECTED[name]),
            "found": sorted({f["code"] for f in r["findings"] if f["severity"] != "suspect"}),
            "missing": missing, "unexpected": unexpected, "recommendation": r["recommendation_label"],
            "overall": r["overall"], "claimed": r.get("claimed_metric"), "reproduced": r.get("reproduced_metric"),
            "holdout": r.get("holdout_metric"), "seconds": round(seconds, 1)}


def run(task, submissions_dir: Path, runner, out: Path, names=None, on_row=None) -> tuple[dict, dict]:
    """Evaluate every benchmark submission. Returns (log, reports by name)."""
    from . import pipeline
    out.mkdir(parents=True, exist_ok=True)
    names = [n for n in EXPECTED if not names or n in names]
    rows, reports, t0 = [], {}, time.time()
    started = datetime.now(timezone.utc)
    for name in names:
        s0 = time.time()
        r = pipeline.evaluate(task, submissions_dir / name, runner, out / f"{name}_runs")
        reports[name] = r
        rows.append(row(name, r, time.time() - s0))
        if on_row:
            on_row(rows[-1])
    passed = sum(x["pass"] for x in rows)
    log = {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "started": started.isoformat(timespec="seconds"),
           "runner": runner.name, "task": task.name, "passed": passed, "total": len(rows),
           "seconds": round(time.time() - t0, 1), "rows": rows}
    return log, reports
