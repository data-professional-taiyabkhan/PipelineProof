"""Employer workspace: assessments, candidates, reports, reviews, team, audit log."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, time, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import audit, db, jobs, storage, tasks_registry
from ..audit import ACTION_LABELS
from ..config import settings
from ..rendering import LONDON, NAMES, render, report_view, summary
from ..security import new_token
from .common import (app_ctx, csrf, evaluations_this_period, need, org_assessment, org_submission, public_base,
                     read_flash, redirect)

router = APIRouter(prefix="/app")
FINAL = {"scored", "unscored", "error", "preflight_failed", "preflight_passed"}


def page(request: Request, ctx, name: str, **data):
    return render(request, name, ctx=ctx, flash=read_flash(request), **data)


# ------------------------------------------------------------------ dashboard
@router.get("")
def dashboard(request: Request):
    ctx = app_ctx(request)
    rows = db.all("""
        select a.*, t.title as task_title,
          (select count(*) from pp.invites i where i.assessment_id = a.id and i.status <> 'withdrawn') as invited,
          (select count(distinct s.invite_id) from pp.submissions s where s.assessment_id = a.id
              and s.submitted_at is not null) as submitted,
          (select count(*) from pp.submissions s where s.assessment_id = a.id and s.status in ('scored', 'unscored')
              and not exists (select 1 from pp.reviews r where r.submission_id = s.id)) as awaiting_review,
          (select count(*) from pp.submissions s where s.assessment_id = a.id and s.status in ('queued', 'running')) as running
        from pp.assessments a join pp.tasks t on t.id = a.task_id
        where a.org_id = %s order by a.status, a.created_at desc""", (ctx.org["id"],))
    open_contests = db.scalar("""select count(*) from pp.contests c join pp.submissions s on s.id = c.submission_id
                                 where s.org_id = %s and c.status = 'open'""", (ctx.org["id"],))
    used, cap = evaluations_this_period(ctx.org)
    return page(request, ctx, "app/dashboard.html", assessments=rows, open_contests=open_contests, used=used, cap=cap,
                nav="assessments")


# ------------------------------------------------------------------ assessments
@router.get("/assessments/new")
def new_assessment(request: Request):
    ctx = app_ctx(request)
    need(ctx, "assess")
    tasks = db.all("select * from pp.tasks where active order by title")
    return page(request, ctx, "app/assessment_new.html", tasks=tasks, nav="assessments")


@router.post("/assessments")
def create_assessment(request: Request, title: str = Form(...), role_title: str = Form(...), task_id: str = Form(...),
                      deadline: str = Form(""), csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "assess")
    csrf(ctx, csrf_token)
    title, role_title = title.strip()[:160], role_title.strip()[:160]
    if not title or not role_title:
        raise HTTPException(400, "Give the assessment a title and a role.")
    if not db.one("select 1 from pp.tasks where id = %s and active", (task_id,)):
        raise HTTPException(400, "Choose a task from the library.")
    dl = None
    if deadline:
        try:
            dl = datetime.combine(datetime.strptime(deadline, "%Y-%m-%d").date(), time(23, 59), LONDON)
        except ValueError:
            raise HTTPException(400, "The deadline is not a valid date.") from None
    a = db.one("""insert into pp.assessments (org_id, task_id, title, role_title, deadline, created_by)
                  values (%s, %s, %s, %s, %s, %s) returning id""",
               (ctx.org["id"], task_id, title, role_title, dl, ctx.actor_id))
    audit.by_ctx(ctx, "assessment.created", "assessment", a["id"], {"title": title, "task": task_id})
    return redirect(f"/app/assessments/{a['id']}", "Assessment created. Invite your first candidate below.")


def _candidates(aid) -> list[dict]:
    return db.all("""
        select i.*, s.id as sub_id, s.status as sub_status, s.submitted_at, s.created_at as sub_created, s.source,
               r.overall, r.recommendation,
               (select json_build_object('decision', rv.decision, 'outcome', rv.outcome, 'at', rv.created_at,
                                         'by', rv.reviewer_label)
                  from pp.reviews rv where rv.submission_id = s.id order by rv.created_at desc limit 1) as review,
               (select released_at from pp.feedback f where f.submission_id = s.id) as feedback_at,
               (select count(*) from pp.contests ct where ct.submission_id = s.id and ct.status = 'open') as open_contests,
               (select count(*) from pp.submissions s2 where s2.invite_id = i.id) as n_submissions
        from pp.invites i
        left join lateral (select * from pp.submissions s where s.invite_id = i.id
                           order by (s.submitted_at is null), s.submitted_at desc nulls last, s.created_at desc
                           limit 1) s on true
        left join pp.reports r on r.submission_id = s.id
        where i.assessment_id = %s
        order by i.created_at""", (aid,))


@router.get("/assessments/{aid}")
def assessment(request: Request, aid: str):
    ctx = app_ctx(request)
    a = org_assessment(ctx, aid)
    return page(request, ctx, "app/assessment.html", a=a, candidates=_candidates(a["id"]), base=public_base(request),
                nav="assessments")


@router.post("/assessments/{aid}/status")
def assessment_status(request: Request, aid: str, status: str = Form(...), csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "assess")
    csrf(ctx, csrf_token)
    a = org_assessment(ctx, aid)
    if status not in ("open", "closed"):
        raise HTTPException(400)
    db.run("update pp.assessments set status = %s, closed_at = case when %s = 'closed' then now() else null end where id = %s",
           (status, status, a["id"]))
    audit.by_ctx(ctx, "assessment.closed" if status == "closed" else "assessment.reopened", "assessment", a["id"])
    msg = ("Assessment closed. Candidates can no longer upload. Personal data will be deleted "
           f"{ctx.org['retention_days']} days from now.") if status == "closed" else "Assessment reopened."
    return redirect(f"/app/assessments/{a['id']}", msg)


@router.post("/assessments/{aid}/invites")
def create_invite(request: Request, aid: str, candidate_name: str = Form(...), candidate_email: str = Form(""),
                  csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "invite")
    csrf(ctx, csrf_token)
    a = org_assessment(ctx, aid)
    if a["status"] != "open":
        raise HTTPException(400, "This assessment is closed. Reopen it to invite more candidates.")
    name = candidate_name.strip()[:120]
    if not name:
        raise HTTPException(400, "Enter the candidate's name.")
    inv = db.one("""insert into pp.invites (org_id, assessment_id, candidate_name, candidate_email, token, created_by)
                    values (%s, %s, %s, %s, %s, %s) returning id, token""",
                 (ctx.org["id"], a["id"], name, candidate_email.strip()[:200] or None, new_token(24), ctx.actor_id))
    audit.by_ctx(ctx, "invite.created", "invite", inv["id"], {"assessment": str(a["id"])})
    return redirect(f"/app/assessments/{a['id']}#invite-{inv['id']}",
                    f"Invite created for {name}. Copy their private link from the list and send it to them.")


@router.post("/invites/{iid}/withdraw")
def withdraw_invite(request: Request, iid: str, csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "invite")
    csrf(ctx, csrf_token)
    inv = db.one("select * from pp.invites where id = %s and org_id = %s", (iid, ctx.org["id"]))
    if not inv:
        raise HTTPException(404)
    db.run("update pp.invites set status = 'withdrawn' where id = %s", (iid,))
    audit.by_ctx(ctx, "invite.withdrawn", "invite", iid)
    return redirect(f"/app/assessments/{inv['assessment_id']}", "Invite withdrawn. The link no longer works.")


@router.get("/assessments/{aid}/export.csv")
def export_csv(request: Request, aid: str):
    ctx = app_ctx(request)
    need(ctx, "export")
    a = org_assessment(ctx, aid)
    rows = db.all("""
        select i.candidate_name, i.candidate_email, i.status as invite_status, s.submitted_at, s.status, r.report,
               (select row_to_json(x) from (select decision, outcome, reviewer_label, reason, created_at
                   from pp.reviews rv where rv.submission_id = s.id order by created_at desc limit 1) x) as review,
               (select released_at from pp.feedback f where f.submission_id = s.id) as feedback_at
        from pp.invites i
        left join lateral (select * from pp.submissions s where s.invite_id = i.id
                           order by (s.submitted_at is null), s.submitted_at desc nulls last limit 1) s on true
        left join pp.reports r on r.submission_id = s.id
        where i.assessment_id = %s order by i.created_at""", (a["id"],))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["candidate", "email", "invite_status", "submitted_at", "evaluation_status", "recommendation", "overall"] +
               [NAMES[k] for k in NAMES] + ["claimed", "clean_rerun", "holdout", "gap", "serious_findings",
                                            "review_decision", "review_outcome", "reviewer", "review_reason", "feedback_released"])
    for r in rows:
        rep = r["report"] or {}
        sc = rep.get("scores") or {}
        rv = r["review"] or {}
        serious = "; ".join(f["code"] for f in rep.get("findings", []) if f["severity"] in ("critical", "high"))
        w.writerow([r["candidate_name"], r["candidate_email"] or "", r["invite_status"],
                    r["submitted_at"].isoformat() if r["submitted_at"] else "", r["status"] or "",
                    rep.get("recommendation_label", ""), rep.get("overall", "")] +
                   [sc.get(k, "") for k in NAMES] +
                   [rep.get("claimed_metric", ""), rep.get("reproduced_metric", ""), rep.get("holdout_metric", ""),
                    rep.get("gap", ""), serious, rv.get("decision", ""), rv.get("outcome", ""), rv.get("reviewer_label", ""),
                    rv.get("reason", ""), r["feedback_at"].isoformat() if r["feedback_at"] else ""])
    audit.by_ctx(ctx, "export.csv", "assessment", a["id"], {"rows": len(rows)})
    fname = "".join(ch if ch.isalnum() else "_" for ch in a["title"])[:60] or "assessment"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.csv"'})


# ------------------------------------------------------------------ submissions and reports
@router.get("/submissions/{sid}")
def submission(request: Request, sid: str):
    ctx = app_ctx(request)
    s = org_submission(ctx, sid)
    rep = db.one("select report from pp.reports where submission_id = %s", (s["id"],))
    report = rep["report"] if rep else None
    reviews = db.all("select * from pp.reviews where submission_id = %s order by created_at desc", (s["id"],))
    fb = db.one("select * from pp.feedback where submission_id = %s", (s["id"],))
    contests = db.all("select * from pp.contests where submission_id = %s order by created_at", (s["id"],))
    history = db.all("""select id, status, created_at, submitted_at, source from pp.submissions
                        where invite_id = %s order by created_at desc""", (s["invite_id"],))
    return page(request, ctx, "app/submission.html", s=s, report=report, v=report_view(report) if report else None,
                summary=summary(report) if report else "", reviews=reviews, feedback=fb, contests=contests,
                history=history, base=public_base(request), nav="assessments")


@router.get("/submissions/{sid}/progress", response_class=HTMLResponse)
def submission_progress(request: Request, sid: str):
    ctx = app_ctx(request)
    s = org_submission(ctx, sid)
    resp = render(request, "partials/progress.html", ctx=ctx, s=s)
    if s["status"] in ("scored", "unscored", "error"):
        resp.headers["HX-Refresh"] = "true"
    return resp


@router.post("/submissions/{sid}/review")
def review(request: Request, sid: str, decision: str = Form(...), outcome: str = Form(...), reason: str = Form(...),
           csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "review", "Only reviewers and admins can record decisions.")
    csrf(ctx, csrf_token)
    s = org_submission(ctx, sid)
    reason = reason.strip()
    if decision not in ("confirm", "override") or outcome not in ("advance", "hold", "reject"):
        raise HTTPException(400, "Choose a decision and an outcome.")
    if len(reason) < 20:
        raise HTTPException(400, "Give a reason of at least 20 characters. It is kept in the audit log.")
    if s["status"] not in ("scored", "unscored"):
        raise HTTPException(400, "This submission has no report to review yet.")
    with db.tx() as c:
        rv = db.one("""insert into pp.reviews (submission_id, reviewer_id, reviewer_label, decision, outcome, reason)
                       values (%s, %s, %s, %s, %s, %s) returning id""",
                    (s["id"], ctx.actor_id, ctx.label, decision, outcome, reason[:4000]), c=c)
        audit.by_ctx(ctx, "review.recorded", "submission", s["id"],
                     {"review": str(rv["id"]), "decision": decision, "outcome": outcome}, c=c)
    return redirect(f"/app/submissions/{s['id']}#review", "Decision recorded in the audit log.")


@router.post("/submissions/{sid}/feedback")
def release_feedback(request: Request, sid: str, message: str = Form(...), include_findings: str = Form(""),
                     csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "release", "Only reviewers and admins can release feedback.")
    csrf(ctx, csrf_token)
    s = org_submission(ctx, sid)
    if not db.one("select 1 from pp.reviews where submission_id = %s", (s["id"],)):
        raise HTTPException(400, "Record a review decision before releasing feedback.")
    message = message.strip()
    if len(message) < 10:
        raise HTTPException(400, "Write a short message to the candidate.")
    db.run("""insert into pp.feedback (submission_id, message, include_findings, released_by)
              values (%s, %s, %s, %s)
              on conflict (submission_id) do update set message = excluded.message,
                 include_findings = excluded.include_findings, released_by = excluded.released_by, released_at = now()""",
           (s["id"], message[:6000], bool(include_findings), ctx.actor_id))
    audit.by_ctx(ctx, "feedback.released", "submission", s["id"], {"include_findings": bool(include_findings)})
    return redirect(f"/app/submissions/{s['id']}#feedback", "Feedback released. The candidate can see it on their page.")


@router.post("/contests/{cid}/answer")
def answer_contest(request: Request, cid: str, response: str = Form(...), csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "review", "Only reviewers and admins can answer contests.")
    csrf(ctx, csrf_token)
    c = db.one("""select c.*, s.org_id from pp.contests c join pp.submissions s on s.id = c.submission_id
                  where c.id = %s and s.org_id = %s""", (cid, ctx.org["id"]))
    if not c:
        raise HTTPException(404)
    if len(response.strip()) < 10:
        raise HTTPException(400, "Write a response to the candidate.")
    db.run("""update pp.contests set status = 'answered', response = %s, responded_by = %s, responded_at = now()
              where id = %s""", (response.strip()[:6000], ctx.actor_id, cid))
    audit.by_ctx(ctx, "contest.answered", "submission", c["submission_id"], {"contest": cid})
    return redirect(f"/app/submissions/{c['submission_id']}#contests", "Response sent to the candidate.")


@router.post("/submissions/{sid}/rerun")
def rerun(request: Request, sid: str, csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "rerun", "Only admins can re-run an evaluation.")
    csrf(ctx, csrf_token)
    s = org_submission(ctx, sid)
    if s["status"] in ("queued", "running"):
        raise HTTPException(400, "This evaluation is already running.")
    if s["submitted_at"] is None:
        raise HTTPException(400, "The candidate has not submitted this upload.")
    db.run("update pp.submissions set status = 'queued', progress = '[]'::jsonb, error = null, heartbeat_at = now() where id = %s",
           (s["id"],))
    audit.by_ctx(ctx, "submission.rerun", "submission", s["id"])
    jobs.dispatch("evaluate", s["id"])
    return redirect(f"/app/submissions/{s['id']}", "Evaluation started again.")


@router.get("/submissions/{sid}/report.json")
def report_json(request: Request, sid: str):
    ctx = app_ctx(request)
    s = org_submission(ctx, sid)
    rep = db.one("select report from pp.reports where submission_id = %s", (s["id"],))
    if not rep:
        raise HTTPException(404)
    return Response(json.dumps(rep["report"], indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="report_{s["id"]}.json"'})


@router.get("/submissions/{sid}/report.md")
def report_md(request: Request, sid: str):
    ctx = app_ctx(request)
    s = org_submission(ctx, sid)
    rep = db.one("select report from pp.reports where submission_id = %s", (s["id"],))
    if not rep:
        raise HTTPException(404)
    from pipelineproof.report import to_markdown
    text = to_markdown(rep["report"]).replace(f"# Assessment report: {rep['report'].get('submission')}",
                                              f"# Assessment report: {s['candidate_name']}", 1)
    return Response(text, media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="report_{s["id"]}.md"'})


@router.get("/submissions/{sid}/files.zip")
def submission_zip(request: Request, sid: str):
    ctx = app_ctx(request)
    s = org_submission(ctx, sid)
    f = db.one("select data from pp.submission_files where submission_id = %s", (s["id"],))
    if not f:
        raise HTTPException(404)
    return Response(bytes(f["data"]), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="submission_{s["id"]}.zip"'})


# ------------------------------------------------------------------ team
@router.get("/team")
def team(request: Request):
    ctx = app_ctx(request)
    if ctx.is_demo:
        need(ctx, "team", "The assessor demo has no team to manage.")
    members = db.all("""select u.id, u.name, u.email, m.role, m.created_at, u.last_login_at from pp.memberships m
                        join pp.users u on u.id = m.user_id where m.org_id = %s order by u.name""", (ctx.org["id"],))
    pending = db.all("select * from pp.pending_members where org_id = %s order by created_at", (ctx.org["id"],))
    return page(request, ctx, "app/team.html", members=members, pending=pending, nav="team")


@router.post("/team")
def add_member(request: Request, email: str = Form(...), role: str = Form(...), csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "team", "Only admins can manage the team.")
    csrf(ctx, csrf_token)
    email = email.strip().lower()
    if role not in ("admin", "reviewer", "recruiter") or "@" not in email:
        raise HTTPException(400, "Enter an email address and choose a role.")
    user = db.one("select id, name from pp.users where email = %s", (email,))
    if user:
        db.run("""insert into pp.memberships (org_id, user_id, role) values (%s, %s, %s)
                  on conflict (org_id, user_id) do update set role = excluded.role""", (ctx.org["id"], user["id"], role))
        audit.by_ctx(ctx, "member.added", "user", user["id"], {"role": role})
        msg = f"{user['name']} now has access as {role}."
    else:
        db.run("""insert into pp.pending_members (org_id, email, role, invited_by) values (%s, %s, %s, %s)
                  on conflict (org_id, email) do update set role = excluded.role""", (ctx.org["id"], email, role, ctx.actor_id))
        audit.by_ctx(ctx, "member.pending", "email", None, {"role": role})
        msg = f"{email} will join as {role} when they create an account with that email at {public_base(request)}/signup."
    return redirect("/app/team", msg)


@router.post("/team/{uid}/role")
def change_role(request: Request, uid: str, role: str = Form(...), csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "team", "Only admins can manage the team.")
    csrf(ctx, csrf_token)
    if role not in ("admin", "reviewer", "recruiter"):
        raise HTTPException(400)
    admins = db.scalar("select count(*) from pp.memberships where org_id = %s and role = 'admin'", (ctx.org["id"],))
    current = db.one("select role from pp.memberships where org_id = %s and user_id = %s", (ctx.org["id"], uid))
    if not current:
        raise HTTPException(404)
    if current["role"] == "admin" and role != "admin" and admins <= 1:
        raise HTTPException(400, "Every organisation needs at least one admin.")
    db.run("update pp.memberships set role = %s where org_id = %s and user_id = %s", (role, ctx.org["id"], uid))
    audit.by_ctx(ctx, "member.role_changed", "user", uid, {"role": role})
    return redirect("/app/team", "Role updated.")


@router.post("/team/{uid}/remove")
def remove_member(request: Request, uid: str, csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "team", "Only admins can manage the team.")
    csrf(ctx, csrf_token)
    current = db.one("select role from pp.memberships where org_id = %s and user_id = %s", (ctx.org["id"], uid))
    if not current:
        raise HTTPException(404)
    admins = db.scalar("select count(*) from pp.memberships where org_id = %s and role = 'admin'", (ctx.org["id"],))
    if current["role"] == "admin" and admins <= 1:
        raise HTTPException(400, "Every organisation needs at least one admin.")
    db.run("delete from pp.memberships where org_id = %s and user_id = %s", (ctx.org["id"], uid))
    audit.by_ctx(ctx, "member.removed", "user", uid)
    return redirect("/app/team", "Team member removed.")


@router.post("/team/pending/remove")
def remove_pending(request: Request, email: str = Form(...), csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "team", "Only admins can manage the team.")
    csrf(ctx, csrf_token)
    db.run("delete from pp.pending_members where org_id = %s and email = %s", (ctx.org["id"], email.strip().lower()))
    return redirect("/app/team", "Invitation cancelled.")


# ------------------------------------------------------------------ audit, tasks, rubric
@router.get("/audit")
def audit_log(request: Request):
    ctx = app_ctx(request)
    rows = db.all("select * from pp.audit_log where org_id = %s order by at desc limit 300", (ctx.org["id"],))
    return page(request, ctx, "app/audit.html", rows=rows, labels=ACTION_LABELS, nav="audit")


@router.get("/tasks")
def task_library(request: Request):
    ctx = app_ctx(request)
    return page(request, ctx, "app/tasks.html", tasks=db.all("select * from pp.tasks where active order by title"), nav="tasks")


@router.get("/tasks/{tid}")
def task_page(request: Request, tid: str):
    ctx = app_ctx(request)
    t = db.one("select * from pp.tasks where id = %s", (tid,))
    if not t:
        raise HTTPException(404)
    return page(request, ctx, "app/task.html", t=t, brief=tasks_registry.doc(tid, "BRIEF.md"),
                dictionary=tasks_registry.doc(tid, "DATA_DICTIONARY.md"),
                notes=tasks_registry.doc(tid, "EMPLOYER_NOTES.md"), stats=tasks_registry.stats(tid), nav="tasks")


@router.get("/rubric")
def rubric(request: Request):
    ctx = app_ctx(request)
    from pipelineproof import scoring
    return page(request, ctx, "app/rubric.html", weights=scoring.WEIGHTS, deductions=scoring.DEDUCTIONS,
                bands=scoring.GAP_BANDS, version=scoring.RUBRIC_VERSION, nav="rubric")


# ------------------------------------------------------------------ assessor demo: live benchmark runs
LIVE_TITLE = "Live benchmark runs (sample)"


def _benchmark_names() -> list[str]:
    from pipelineproof.benchmark import EXPECTED
    d = settings.benchmark_dir / "submissions"
    return [n for n in EXPECTED if (d / n).is_dir()]


def _live_assessment(ctx) -> dict:
    a = db.one("select * from pp.assessments where org_id = %s and title = %s", (ctx.org["id"], LIVE_TITLE))
    if a:
        return a
    task = db.one("select id from pp.tasks where active order by id limit 1")
    return db.one("""insert into pp.assessments (org_id, task_id, title, role_title)
                     values (%s, %s, %s, 'Machine learning engineer') returning *""", (ctx.org["id"], task["id"], LIVE_TITLE))


@router.get("/demo-runs")
def demo_runs(request: Request):
    ctx = app_ctx(request)
    if not ctx.org["is_demo"]:
        raise HTTPException(404)
    from pipelineproof.benchmark import EXPECTED, PLANTED
    recent = db.all("""select s.id, s.label, s.status, s.created_at, r.recommendation, r.overall
                       from pp.submissions s left join pp.reports r on r.submission_id = s.id
                       where s.org_id = %s and s.source = 'demo' order by s.created_at desc limit 12""", (ctx.org["id"],))
    used, cap = evaluations_this_period(ctx.org)
    rows = [{"name": n, "planted": PLANTED.get(n, ""), "expected": sorted(EXPECTED[n])} for n in _benchmark_names()]
    return page(request, ctx, "app/demo_runs.html", rows=rows, recent=recent, used=used, cap=cap, nav="demo")


@router.post("/demo-runs/{name}")
def start_demo_run(request: Request, name: str, csrf_token: str = Form("")):
    ctx = app_ctx(request)
    need(ctx, "demo_run")
    csrf(ctx, csrf_token)
    if not ctx.org["is_demo"] or name not in _benchmark_names():
        raise HTTPException(404)
    used, cap = evaluations_this_period(ctx.org)
    if used >= cap:
        raise HTTPException(429, "The demo has reached today's limit of live evaluations. The sample reports are "
                                 "still available, and the limit resets within 24 hours.")
    from ..security import client_ip
    if not db.rate_ok("demo_run", client_ip(request), 6, 3600):
        raise HTTPException(429, "You have started several live runs in the last hour. Try again a little later.")
    a = _live_assessment(ctx)
    inv = db.one("select * from pp.invites where assessment_id = %s and candidate_name = %s", (a["id"], f"Benchmark {name}"))
    if not inv:
        inv = db.one("""insert into pp.invites (org_id, assessment_id, candidate_name, token, is_synthetic, status, consent_at)
                        values (%s, %s, %s, %s, true, 'submitted', now()) returning *""",
                     (ctx.org["id"], a["id"], f"Benchmark {name}", new_token(24)))
    data = storage.zip_folder(settings.benchmark_dir / "submissions" / name)
    s = db.one("""insert into pp.submissions (org_id, assessment_id, invite_id, source, label, filename, size_bytes, sha256,
                                              status, submitted_at)
                  values (%s, %s, %s, 'demo', %s, %s, %s, %s, 'queued', now()) returning id""",
               (ctx.org["id"], a["id"], inv["id"], name, f"{name}.zip", len(data), storage.sha256(data)))
    db.run("insert into pp.submission_files (submission_id, data) values (%s, %s)", (s["id"], data))
    audit.by_ctx(ctx, "submission.demo_run", "submission", s["id"], {"benchmark": name})
    jobs.dispatch("evaluate", s["id"])
    return redirect(f"/app/submissions/{s['id']}", f"Live evaluation of {name} started. This page updates as it runs.")


@router.get("/demo-runs/{name}.zip")
def demo_zip(request: Request, name: str):
    ctx = app_ctx(request)
    if not ctx.org["is_demo"] or name not in _benchmark_names():
        raise HTTPException(404)
    return Response(storage.zip_folder(settings.benchmark_dir / "submissions" / name, top=name),
                    media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


_ = timezone
