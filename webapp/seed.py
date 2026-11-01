"""Seed the assessor demo workspace and record benchmark runs.

    python -m webapp.seed [--reset] [--no-eval]        # locally, with the configured runner

On Modal: `modal run modal_app.py::seed` (evaluations run in parallel in Modal containers).
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from . import audit, db, jobs, storage, tasks_registry
from .config import settings
from .security import new_token

log = logging.getLogger("pipelineproof.seed")
DEMO_ORG = "Northwind Broadband (sample)"
SAMPLE_TITLE = "Machine learning engineer take-home (sample)"
SAMPLE = [  # synthetic candidate -> benchmark submission
    ("Amelia Hart", "amelia.hart@example.com", "p01_clean"),
    ("Rohan Mehta", "rohan.mehta@example.com", "p02_target_leak"),
    ("Chloe Okafor", "chloe.okafor@example.com", "p03_random_split"),
    ("Daniel Novak", "daniel.novak@example.com", "p09_tune_on_test"),
    ("Priya Raman", "priya.raman@example.com", "p08_inflated_claim"),
    ("Tom Lindqvist", "tom.lindqvist@example.com", "p14_lightgbm_clean"),
    ("Leo Brennan", "leo.brennan@example.com", "p16_notebook_scaler_first"),
]
REVIEWER = "Sample reviewer"


def demo_org(reset: bool = False) -> dict:
    org = db.one("select * from pp.organisations where is_demo order by created_at limit 1")
    if org and reset:
        db.run("delete from pp.organisations where id = %s", (org["id"],))
        org = None
    if not org:
        org = db.one("""insert into pp.organisations (name, is_demo, retention_days, monthly_evaluation_cap)
                        values (%s, true, 365, 0) returning *""", (DEMO_ORG,))
        audit.log(org["id"], "system", "PipelineProof", "org.created", "organisation", org["id"], {"name": DEMO_ORG})
    return org


def prepare(reset: bool = False) -> tuple[dict, list[str]]:
    """Create the demo organisation, sample assessment and synthetic candidates.
    Returns the organisation and the ids of submissions that still need evaluating."""
    tasks_registry.sync_to_db()
    org = demo_org(reset)
    task_id = "churn_v1" if "churn_v1" in tasks_registry.available() else tasks_registry.available()[0]
    a = db.one("select * from pp.assessments where org_id = %s and title = %s", (org["id"], SAMPLE_TITLE))
    if not a:
        a = db.one("""insert into pp.assessments (org_id, task_id, title, role_title, deadline)
                      values (%s, %s, %s, 'Machine learning engineer', now() + interval '365 days') returning *""",
                   (org["id"], task_id, SAMPLE_TITLE))
    pending = []
    for name, email, bench in SAMPLE:
        folder = settings.benchmark_dir / "submissions" / bench
        if not folder.is_dir():
            log.warning("benchmark submission %s not found, skipping", bench)
            continue
        inv = db.one("select * from pp.invites where assessment_id = %s and candidate_name = %s", (a["id"], name))
        if not inv:
            inv = db.one("""insert into pp.invites (org_id, assessment_id, candidate_name, candidate_email, token, status,
                                                    is_synthetic, consent_at, opened_at)
                            values (%s, %s, %s, %s, %s, 'submitted', true, now(), now()) returning *""",
                         (org["id"], a["id"], name, email, new_token(24)))
        s = db.one("""select id, status from pp.submissions where invite_id = %s and source = 'benchmark'
                      order by created_at desc limit 1""", (inv["id"],))
        if s and s["status"] in ("scored", "unscored"):
            continue
        if not s:
            data = storage.zip_folder(folder)
            s = db.one("""insert into pp.submissions (org_id, assessment_id, invite_id, source, label, filename, size_bytes,
                                                      sha256, status, submitted_at, preflight)
                          values (%s, %s, %s, 'benchmark', %s, 'submission.zip', %s, %s, 'queued', now(), %s) returning id""",
                       (org["id"], a["id"], inv["id"], bench, len(data), storage.sha256(data),
                        db.json({"ok": True, "problems": [], "warnings": [], "note": "seeded"})))
            db.run("insert into pp.submission_files (submission_id, data) values (%s, %s)", (s["id"], data))
        pending.append(str(s["id"]))
    return org, pending


def finish(org: dict):
    """Add sample reviewer activity so every stage of the workflow is visible in the demo."""
    a = db.one("select * from pp.assessments where org_id = %s and title = %s", (org["id"], SAMPLE_TITLE))
    if not a:
        return

    def sub_for(name):
        return db.one("""select s.id, s.status from pp.submissions s join pp.invites i on i.id = s.invite_id
                         where i.assessment_id = %s and i.candidate_name = %s and s.source = 'benchmark'
                         order by s.created_at desc limit 1""", (a["id"], name))

    plans = {
        "Amelia Hart": ("confirm", "advance",
                        "Claim reproduced exactly and held up on unseen customers with a small, expected drop. Split by customer, "
                        "duplicates removed, post-cancellation field excluded with a clear reason in DECISIONS.md.",
                        "Thank you for your submission. Your pipeline reproduced exactly and held up well on customers it had not "
                        "seen. We would like to invite you to a technical interview."),
        "Rohan Mehta": ("confirm", "reject",
                        "The ablation shows the headline AUC depends on retention_offer_sent, which is set after a customer asks "
                        "to cancel. On unseen customers the model scores 0.81, well below the 0.97 claimed.",
                        "Thank you for your work. The result you reported relied on retention_offer_sent, which is only filled in "
                        "after a customer has asked to cancel, so it is not available when the model is used. We will not be "
                        "progressing your application this time; the evidence is below."),
    }
    for name, (decision, outcome, reason, message) in plans.items():
        s = sub_for(name)
        if not s or s["status"] not in ("scored", "unscored"):
            continue
        if not db.one("select 1 from pp.reviews where submission_id = %s", (s["id"],)):
            db.run("""insert into pp.reviews (submission_id, reviewer_label, decision, outcome, reason)
                      values (%s, %s, %s, %s, %s)""", (s["id"], REVIEWER, decision, outcome, reason))
            audit.log(org["id"], "user", REVIEWER, "review.recorded", "submission", s["id"],
                      {"decision": decision, "outcome": outcome})
        if not db.one("select 1 from pp.feedback where submission_id = %s", (s["id"],)):
            db.run("insert into pp.feedback (submission_id, message, include_findings) values (%s, %s, true)",
                   (s["id"], message))
            audit.log(org["id"], "user", REVIEWER, "feedback.released", "submission", s["id"], {"include_findings": True})
    s = sub_for("Rohan Mehta")
    if s and db.one("select 1 from pp.feedback where submission_id = %s", (s["id"],)) and \
            not db.one("select 1 from pp.contests where submission_id = %s", (s["id"],)):
        db.run("""insert into pp.contests (submission_id, message, status, response, responded_at)
                  values (%s, %s, 'answered', %s, now())""",
               (s["id"], "I included retention_offer_sent because it was in the data I was given and the brief did not say to "
                         "exclude any columns. Could you take that into account?",
                "Thank you for raising this. The data dictionary says the field is set by the CRM at month end, after the "
                "snapshot date, and the brief says the model scores customers on the 1st of the month. Spotting that is part "
                "of the task, so our decision stands, but your point about the brief has been passed to the team."))
        audit.log(org["id"], "candidate", "Synthetic candidate", "contest.opened", "submission", s["id"], {})
        audit.log(org["id"], "user", REVIEWER, "contest.answered", "submission", s["id"], {})


def run_local(reset: bool = False, evaluate: bool = True):
    org, pending = prepare(reset)
    if evaluate:
        for sid in pending:
            log.info("evaluating %s", sid)
            jobs.process_safely("evaluate", sid)
    finish(org)
    return org, pending


def record_benchmark(log_dict: dict):
    db.run("""insert into pp.benchmark_runs (runner, passed, total, log, started_at)
              values (%s, %s, %s, %s, %s)""",
           (log_dict["runner"], log_dict["passed"], log_dict["total"], db.json(log_dict),
            log_dict.get("started") or datetime.now(timezone.utc).isoformat()))


def run_benchmark_local():
    from pipelineproof import benchmark
    import tempfile
    from pathlib import Path
    task = tasks_registry.load("churn_v1")
    with tempfile.TemporaryDirectory() as tmp:
        log_dict, _ = benchmark.run(task, settings.benchmark_dir / "submissions", jobs.runner(), Path(tmp),
                                    on_row=lambda r: print(("PASS " if r["pass"] else "FAIL ") + r["submission"], flush=True))
    record_benchmark(jobs._clean(log_dict))
    return log_dict


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--benchmark", action="store_true")
    args = ap.parse_args()
    if args.benchmark:
        out = run_benchmark_local()
        print(f"benchmark {out['passed']}/{out['total']}")
    else:
        o, p = run_local(args.reset, not args.no_eval)
        print(f"demo org {o['name']}: {len(p)} submissions evaluated")
