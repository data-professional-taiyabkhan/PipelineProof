"""Planted-defect benchmark: the acceptance test for the harness.

  python benchmark/run_benchmark.py [--runner docker|local|modal] [--only p02_target_leak,...]

Each submission has a known property. A submission PASSES when every expected finding code
is present and no unexpected blocking finding (critical/high/medium) appears. The run writes
a dated evaluation log to benchmark/logs/.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipelineproof import benchmark, report, task as task_mod  # noqa: E402
from pipelineproof.benchmark import EXPECTED  # noqa: E402,F401 - re-exported for older scripts
from pipelineproof.runners import get_runner  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runner", default="docker", help="docker | local | modal")
    ap.add_argument("--only", default="")
    ap.add_argument("--task", default=str(ROOT / "tasks" / "churn_v1"))
    ap.add_argument("--out", default=str(ROOT / "benchmark" / "out"))
    a = ap.parse_args()
    t = task_mod.load(a.task)
    out = Path(a.out)

    def show(r):
        print(f"{'PASS' if r['pass'] else 'FAIL'}  {r['submission']:28s} {r['recommendation']:34s} "
              f"missing={r['missing']} unexpected={r['unexpected']} ({r['seconds']}s)", flush=True)

    log, reports = benchmark.run(t, ROOT / "benchmark" / "submissions", get_runner(a.runner), out,
                                 names=[n for n in a.only.split(",") if n] or None, on_row=show)
    for name, r in reports.items():
        (out / f"{name}.json").write_text(json.dumps(r, indent=2, default=str))
        (out / f"{name}.md").write_text(report.to_markdown(r))
    logs = ROOT / "benchmark" / "logs"
    logs.mkdir(exist_ok=True)
    (logs / f"evaluation_log_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json").write_text(json.dumps(log, indent=2))
    print(f"\n{log['passed']}/{log['total']} passed in {log['seconds']}s")
    return 0 if log["passed"] == log["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
