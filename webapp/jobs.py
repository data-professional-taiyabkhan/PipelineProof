"""Evaluation jobs.

The web app records a submission, then calls dispatch(kind, submission_id). Where the work runs
depends on the deployment:

  * on Modal, dispatch spawns the `run_job` function in its own container (see modal_app.py),
    which runs candidate code in gVisor sandboxes with networking blocked
  * locally, a small thread pool runs the job in-process with the configured runner

kinds: "preflight" (contract check on a 400-row sample, never touches the holdout) and
"evaluate" (the full report).
"""
from __future__ import annotations

import logging
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import audit, db, storage, tasks_registry
from .config import settings

log = logging.getLogger("pipelineproof.jobs")
SYSTEM = "PipelineProof"

_dispatcher = None
_local_pool: ThreadPoolExecutor | None = None
_runner = None
_runner_lock = threading.Lock()


# ------------------------------------------------------------------ dispatch
def set_dispatcher(fn):
    global _dispatcher
    _dispatcher = fn


def dispatch(kind: str, submission_id) -> None:
    if _dispatcher is not None:
        _dispatcher(kind, str(submission_id))
        return
    global _local_pool
    if _local_pool is None:
        _local_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pp-job")
    _local_pool.submit(process_safely, kind, str(submission_id))


def runner():
    global _runner
    with _runner_lock:
        if _runner is None:
            from pipelineproof.runners import get_runner
            _runner = get_runner(settings.runner)
    return _runner


# ------------------------------------------------------------------ processing
def _clean(obj):
    """Make a report JSON-safe: numpy scalars to Python, NaN and infinity to null."""
    import math

    def walk(o):
        if isinstance(o, dict):
            return {str(k): walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple, set)):
            return [walk(v) for v in o]
        if isinstance(o, float):
            return None if math.isnan(o) or math.isinf(o) else o
        if o is None or isinstance(o, (str, int, bool)):
            return o
        try:
            import numpy as np
            if isinstance(o, np.bool_):
                return bool(o)
            if isinstance(o, np.integer):
                return int(o)
            if isinstance(o, np.floating):
                return walk(float(o))
            if isinstance(o, np.ndarray):
                return walk(o.tolist())
        except ImportError:
            pass
        return str(o)

    return walk(obj)


def _load(sid: str) -> dict:
    row = db.one("""select s.*, a.task_id, f.data
                    from pp.submissions s
                    join pp.assessments a on a.id = s.assessment_id
                    join pp.submission_files f on f.submission_id = s.id
                    where s.id = %s""", (sid,))
    if row is None:
        raise LookupError(f"submission {sid} not found")
    return row


class _Heartbeat:
    def __init__(self, sid: str, every: float = 20.0):
        self.sid, self.every, self.stop = sid, every, threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stop.wait(self.every):
            try:
                db.run("update pp.submissions set heartbeat_at = now() where id = %s", (self.sid,))
            except Exception:  # noqa: BLE001
                log.warning("heartbeat failed", exc_info=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()


def run_preflight(sid: str) -> dict:
    s = _load(sid)
    db.run("update pp.submissions set status = 'preflight_running', heartbeat_at = now(), error = null where id = %s", (sid,))
    from pipelineproof import pipeline
    task = tasks_registry.load(s["task_id"])
    with _Heartbeat(sid), tempfile.TemporaryDirectory(prefix="pp_pf_") as tmp:
        root = storage.extract(bytes(s["data"]), Path(tmp) / "sub")
        res = pipeline.preflight(task, root, runner(), Path(tmp) / "work")
    res = _clean(res)
    status = "preflight_passed" if res["ok"] else "preflight_failed"
    db.run("update pp.submissions set status = %s, preflight = %s, heartbeat_at = now() where id = %s",
           (status, db.json(res), sid))
    audit.log(s["org_id"], "system", SYSTEM, f"submission.{status}", "submission", sid,
              {"problems": len(res["problems"]), "warnings": len(res["warnings"])})
    return res


def run_evaluation(sid: str) -> dict:
    s = _load(sid)
    db.run("""update pp.submissions set status = 'running', started_at = now(), finished_at = null,
                  progress = '[]'::jsonb, error = null, heartbeat_at = now() where id = %s""", (sid,))
    audit.log(s["org_id"], "system", SYSTEM, "evaluation.started", "submission", sid, {})
    from pipelineproof import pipeline

    def progress(stage: str, message: str):
        item = [{"stage": stage, "message": message, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}]
        db.run("update pp.submissions set progress = progress || %s, heartbeat_at = now() where id = %s",
               (db.json(item), sid))

    task = tasks_registry.load(s["task_id"])
    with _Heartbeat(sid), tempfile.TemporaryDirectory(prefix="pp_ev_") as tmp:
        root = storage.extract(bytes(s["data"]), Path(tmp) / "sub")
        report = pipeline.evaluate(task, root, runner(), Path(tmp) / "work", progress=progress)
    report["submission"] = s["label"] or s["filename"]
    report = _clean(report)
    status = "scored" if report.get("status") == "scored" else "unscored"
    with db.tx() as c:
        db.run("""insert into pp.reports (submission_id, report, overall, recommendation, rubric_version)
                  values (%s, %s, %s, %s, %s)
                  on conflict (submission_id) do update set report = excluded.report, overall = excluded.overall,
                     recommendation = excluded.recommendation, rubric_version = excluded.rubric_version,
                     created_at = now()""",
               (sid, db.json(report), report.get("overall"), report.get("recommendation"),
                report.get("rubric", {}).get("version")), c=c)
        db.run("update pp.submissions set status = %s, finished_at = now(), heartbeat_at = now() where id = %s",
               (status, sid), c=c)
        audit.log(s["org_id"], "system", SYSTEM, "evaluation.finished", "submission", sid,
                  {"recommendation": report.get("recommendation"), "overall": report.get("overall"),
                   "runtime_s": report.get("runtime_s")}, c=c)
    return report


def process(kind: str, sid: str):
    if kind == "preflight":
        return run_preflight(sid)
    if kind == "evaluate":
        return run_evaluation(sid)
    raise ValueError(f"unknown job kind {kind!r}")


def process_safely(kind: str, sid: str):
    try:
        return process(kind, sid)
    except Exception as e:  # noqa: BLE001 - record any failure on the submission
        log.error("job %s %s failed: %s", kind, sid, traceback.format_exc())
        msg = f"{type(e).__name__}: {e}"[:500]
        try:
            row = db.one("select org_id from pp.submissions where id = %s", (sid,))
            status = "preflight_failed" if kind == "preflight" else "error"
            pf = db.json({"ok": False, "problems": ["The pre-flight check could not run. Please try again; "
                                                     "if it keeps failing, contact the employer."],
                          "warnings": []}) if kind == "preflight" else None
            db.run("""update pp.submissions set status = %s, error = %s, finished_at = now(),
                          preflight = coalesce(%s, preflight) where id = %s""", (status, msg, pf, sid))
            if row:
                audit.log(row["org_id"], "system", SYSTEM, "evaluation.error", "submission", sid, {"kind": kind, "error": msg})
        except Exception:  # noqa: BLE001
            log.error("could not record job failure", exc_info=True)
        return None


# ------------------------------------------------------------------ maintenance
def maintenance() -> dict:
    """Daily housekeeping: stale jobs, retention deletion, demo clean-up, expired sessions."""
    out = {}
    out["stale_jobs"] = db.run("""update pp.submissions set status = case when status = 'preflight_running'
                                       then 'preflight_failed' else 'error' end,
                                     error = 'The job stopped responding and was stopped', finished_at = now()
                                   where status in ('running', 'preflight_running', 'queued')
                                     and coalesce(heartbeat_at, created_at) < now() - interval '45 minutes'""")
    # personal data is deleted once an assessment has been closed for longer than the retention period
    rows = db.all("""select i.id, i.org_id from pp.invites i
                     join pp.assessments a on a.id = i.assessment_id
                     join pp.organisations o on o.id = a.org_id
                     where not o.is_demo and a.status = 'closed'
                       and a.closed_at < now() - make_interval(days => o.retention_days)""")
    for r in rows:
        db.run("delete from pp.invites where id = %s", (r["id"],))
    by_org = {}
    for r in rows:
        by_org[r["org_id"]] = by_org.get(r["org_id"], 0) + 1
    for org, n in by_org.items():
        audit.log(org, "system", SYSTEM, "retention.deleted", "invite", None, {"invites": n})
    out["retention_invites_deleted"] = len(rows)
    # assessor demo: remove visitors' live runs and uploads after a day, keep the seeded examples
    out["demo_cleanup"] = db.run("""delete from pp.submissions s using pp.organisations o
                                    where o.id = s.org_id and o.is_demo and s.source <> 'benchmark'
                                      and s.created_at < now() - interval '1 day'""")
    db.run("""delete from pp.reviews r using pp.submissions s, pp.organisations o
              where s.id = r.submission_id and o.id = s.org_id and o.is_demo and r.reviewer_id is null
                and r.reviewer_label <> 'Sample reviewer' and r.created_at < now() - interval '3 days'""")
    db.run("""delete from pp.invites i using pp.organisations o
              where o.id = i.org_id and o.is_demo and not i.is_synthetic and i.created_at < now() - interval '1 day'""")
    db.run("""delete from pp.assessments a using pp.organisations o
              where o.id = a.org_id and o.is_demo and a.created_at < now() - interval '1 day'
                and a.title not like '%%(sample)'""")
    out["sessions_deleted"] = db.run("delete from pp.sessions where expires_at < now()")
    db.run("delete from pp.rate_events where at < now() - interval '3 days'")
    return out
