"""Command line: evaluate a submission, run the pre-flight check, or run the benchmark.

  python -m pipelineproof evaluate <task_dir> <submission_dir> [--out DIR] [--runner docker|modal]
  python -m pipelineproof preflight <task_dir> <submission_dir> [--runner ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pipeline, report, task as task_mod
from .runners import get_runner


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipelineproof")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("evaluate", "preflight"):
        p = sp.add_parser(name)
        p.add_argument("task")
        p.add_argument("submission")
        p.add_argument("--out", default="out")
        p.add_argument("--runner", default="docker", help="docker | local (no isolation, demos only) | modal")
    a = ap.parse_args(argv)
    t = task_mod.load(a.task)
    runner = get_runner(a.runner)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    name = Path(a.submission).name
    if a.cmd == "preflight":
        res = pipeline.preflight(t, a.submission, runner, out / f"{name}_preflight")
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1
    r = pipeline.evaluate(t, a.submission, runner, out / f"{name}_runs", progress=lambda st, m: print(f"  - {m}", flush=True))
    (out / f"{name}.json").write_text(json.dumps(r, indent=2, default=str))
    (out / f"{name}.md").write_text(report.to_markdown(r))
    print(report.summary(r))
    print(f"{r['recommendation_label']} | overall {r['overall']} | report: {out / (name + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
