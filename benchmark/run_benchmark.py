"""Planted-defect benchmark: the acceptance test for the harness.

  python benchmark/run_benchmark.py [--runner docker] [--only p02_target_leak,...]

Each submission has a known property. A submission PASSES when every expected finding code
is present and no unexpected blocking finding (critical/high/medium) appears. The run writes
a dated evaluation log to benchmark/logs/, which is the evidence for the plan's Appendix N.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipelineproof import pipeline, report, task as task_mod  # noqa: E402
from pipelineproof.runners import get_runner  # noqa: E402

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
}
BLOCKING = {"critical", "high", "medium"}
# findings that legitimately accompany an expected one
COMPANIONS = {"LEAK_ABLATION": {"REPRO_CLAIM_MISMATCH"},
              "REPRO_NONDETERMINISTIC": {"REPRO_CLAIM_MISMATCH"}}   # an unseeded claim cannot reproduce


def judge(name, r):
    codes = {f["code"] for f in r["findings"]}
    blocking = {f["code"] for f in r["findings"] if f["severity"] in BLOCKING}
    want = EXPECTED[name]
    allowed = set(want) | set().union(*(COMPANIONS.get(c, set()) for c in want)) if want else set()
    missing = want - codes
    unexpected = blocking - allowed
    return not missing and not unexpected, sorted(missing), sorted(unexpected)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runner", default="docker", help="docker | local | modal")
    ap.add_argument("--only", default="")
    ap.add_argument("--task", default=str(ROOT / "tasks" / "churn_v1"))
    ap.add_argument("--out", default=str(ROOT / "benchmark" / "out"))
    a = ap.parse_args()
    t = task_mod.load(a.task)
    runner = get_runner(a.runner)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    names = [n for n in EXPECTED if not a.only or n in a.only.split(",")]
    rows, t0 = [], time.time()
    for name in names:
        s0 = time.time()
        r = pipeline.evaluate(t, ROOT / "benchmark" / "submissions" / name, runner, out / f"{name}_runs")
        (out / f"{name}.json").write_text(json.dumps(r, indent=2, default=str))
        (out / f"{name}.md").write_text(report.to_markdown(r))
        ok, missing, unexpected = judge(name, r)
        rows.append({"submission": name, "pass": ok, "expected": sorted(EXPECTED[name]), "missing": missing,
                     "unexpected": unexpected, "recommendation": r["recommendation_label"], "overall": r["overall"],
                     "claimed": r.get("claimed_metric"), "reproduced": r.get("reproduced_metric"),
                     "holdout": r.get("holdout_metric"), "seconds": round(time.time() - s0, 1)})
        print(f"{'PASS' if ok else 'FAIL'}  {name:28s} {r['recommendation_label']:34s} "
              f"missing={missing} unexpected={unexpected} ({rows[-1]['seconds']}s)", flush=True)
    passed = sum(r["pass"] for r in rows)
    log = {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "runner": runner.name, "task": t.name,
           "passed": passed, "total": len(rows), "seconds": round(time.time() - t0, 1), "rows": rows}
    logs = ROOT / "benchmark" / "logs"
    logs.mkdir(exist_ok=True)
    (logs / f"evaluation_log_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json").write_text(json.dumps(log, indent=2))
    print(f"\n{passed}/{len(rows)} passed in {log['seconds']}s")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
