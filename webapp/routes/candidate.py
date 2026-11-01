"""Candidate portal, reached through the private link in an invite. No account needed."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from .. import audit, db, jobs, storage, tasks_registry
from ..config import settings
from ..rendering import render, report_view
from ..security import client_ip
from .common import evaluations_this_period, redirect

router = APIRouter(prefix="/c")


def _invite(token: str) -> dict:
    if not token or len(token) > 64:
        raise HTTPException(404)
    inv = db.one("""select i.*, a.title as assessment_title, a.role_title, a.deadline, a.status as assessment_status,
                           a.task_id, o.name as org_name, o.is_demo, o.retention_days, o.monthly_evaluation_cap,
                           o.id as org_id
                    from pp.invites i join pp.assessments a on a.id = i.assessment_id
                    join pp.organisations o on o.id = i.org_id where i.token = %s""", (token,))
    if not inv or inv["status"] == "withdrawn":
        raise HTTPException(404)
    return inv


def _actor(inv) -> str:
    return "Synthetic candidate" if inv["is_synthetic"] else "Candidate"


def _closed(inv) -> str | None:
    if inv["assessment_status"] != "open":
        return "This assessment is closed."
    from datetime import datetime, timezone
    if inv["deadline"] and inv["deadline"] < datetime.now(timezone.utc):
        return "The deadline for this assessment has passed."
    return None


def _state(inv) -> dict:
    subs = db.all("""select id, status, filename, size_bytes, preflight, created_at, submitted_at, error, source
                     from pp.submissions where invite_id = %s order by created_at desc""", (inv["id"],))
    submitted = next((s for s in subs if s["submitted_at"]), None)
    latest = subs[0] if subs else None
    feedback = None
    if submitted:
        fb = db.one("select * from pp.feedback where submission_id = %s", (submitted["id"],))
        if fb:
            rep = db.one("select report from pp.reports where submission_id = %s", (submitted["id"],))
            feedback = {"message": fb["message"], "released_at": fb["released_at"],
                        "include_findings": fb["include_findings"],
                        "report": rep["report"] if rep and fb["include_findings"] else None}
            if feedback["report"]:
                feedback["v"] = report_view(feedback["report"])
    contests = db.all("select * from pp.contests where submission_id = %s order by created_at",
                      (submitted["id"],)) if submitted else []
    return {"subs": subs, "submitted": submitted, "latest": latest, "feedback": feedback, "contests": contests}


@router.get("/{token}")
def portal(request: Request, token: str):
    inv = _invite(token)
    if inv["opened_at"] is None:
        db.run("update pp.invites set opened_at = now(), status = case when status = 'invited' then 'opened' else status end "
               "where id = %s", (inv["id"],))
        audit.log(inv["org_id"], "candidate", _actor(inv), "invite.opened", "invite", inv["id"])
    st = _state(inv)
    m = tasks_registry.meta(inv["task_id"])
    return render(request, "candidate/portal.html", inv=inv, task=m, st=st, closed=_closed(inv),
                  brief=tasks_registry.doc(inv["task_id"], "BRIEF.md"),
                  dictionary=tasks_registry.doc(inv["task_id"], "DATA_DICTIONARY.md"),
                  max_mb=settings.max_upload_mb, flash=_flash(request))


def _flash(request):
    from .common import read_flash
    return read_flash(request)


@router.post("/{token}/consent")
def consent(request: Request, token: str, agree: str = Form("")):
    inv = _invite(token)
    if not agree:
        raise HTTPException(400, "Tick the box to confirm you have read the notice.")
    if inv["consent_at"] is None:
        db.run("update pp.invites set consent_at = now() where id = %s", (inv["id"],))
        audit.log(inv["org_id"], "candidate", _actor(inv), "candidate.consented", "invite", inv["id"])
    return redirect(f"/c/{token}#task", "Thank you. The task and data are below.")


@router.get("/{token}/train.csv")
def train_csv(token: str):
    inv = _invite(token)
    if inv["consent_at"] is None:
        raise HTTPException(403, "Read and accept the notice first.")
    return FileResponse(tasks_registry.train_csv_path(inv["task_id"]), media_type="text/csv", filename="train.csv")


@router.get("/{token}/starter-template.zip")
def template(token: str):
    inv = _invite(token)
    if inv["consent_at"] is None:
        raise HTTPException(403, "Read and accept the notice first.")
    return Response(tasks_registry.template_zip(inv["task_id"]), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="starter-template.zip"'})


@router.post("/{token}/upload")
async def upload(request: Request, token: str, file: UploadFile = File(...)):
    inv = _invite(token)
    if inv["consent_at"] is None:
        raise HTTPException(403, "Read and accept the notice first.")
    if (msg := _closed(inv)):
        raise HTTPException(400, msg)
    st = _state(inv)
    if st["submitted"]:
        raise HTTPException(400, "You have already submitted. Your submission is locked.")
    if not db.rate_ok("upload", f"{inv['id']}", 15, 3600) or not db.rate_ok("upload_ip", client_ip(request), 40, 3600):
        raise HTTPException(429, "Too many uploads in the last hour. Wait a little and try again.")
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"The zip is larger than {settings.max_upload_mb} MB. Leave the data out of your "
                                 f"submission; it is provided when your code runs.")
    try:
        files = storage.inspect(data)
    except storage.BadArchive as e:
        raise HTTPException(400, f"{e}. Upload a .zip of your submission folder.") from None
    lower = {f.lower() for f in files}
    if any(f.endswith("train.csv") for f in lower):
        files_note = "Your zip contains train.csv. It is not needed and will be ignored when your code runs."
    else:
        files_note = None
    fname = (file.filename or "submission.zip")[-120:]
    s = db.one("""insert into pp.submissions (org_id, assessment_id, invite_id, source, filename, size_bytes, sha256, status)
                  values (%s, %s, %s, 'candidate', %s, %s, %s, 'uploaded') returning id""",
               (inv["org_id"], inv["assessment_id"], inv["id"], fname, len(data), storage.sha256(data)))
    db.run("insert into pp.submission_files (submission_id, data) values (%s, %s)", (s["id"], data))
    audit.log(inv["org_id"], "candidate", _actor(inv), "submission.uploaded", "submission", s["id"],
              {"bytes": len(data), "files": len(files)})
    db.run("update pp.submissions set status = 'preflight_running', heartbeat_at = now() where id = %s", (s["id"],))
    jobs.dispatch("preflight", s["id"])
    return redirect(f"/c/{token}#upload", files_note or "Uploaded. We are running a quick check on a small sample.",
                    "warn" if files_note else "ok")


@router.get("/{token}/status", response_class=HTMLResponse)
def status(request: Request, token: str):
    inv = _invite(token)
    st = _state(inv)
    resp = render(request, "partials/candidate_status.html", inv=inv, st=st)
    latest = st["latest"]
    busy = latest and latest["status"] in ("preflight_running", "queued", "running", "uploaded")
    if not busy:
        resp.headers["HX-Refresh"] = "true"
    return resp


@router.post("/{token}/submit/{sid}")
def submit(request: Request, token: str, sid: str, confirm: str = Form("")):
    inv = _invite(token)
    if (msg := _closed(inv)):
        raise HTTPException(400, msg)
    if not confirm:
        raise HTTPException(400, "Tick the box to confirm this is your final submission.")
    st = _state(inv)
    if st["submitted"]:
        raise HTTPException(400, "You have already submitted.")
    s = next((x for x in st["subs"] if str(x["id"]) == sid), None)
    if not s or s["status"] != "preflight_passed":
        raise HTTPException(400, "Only an upload that passed the pre-flight check can be submitted.")
    org = {"id": inv["org_id"], "is_demo": inv["is_demo"], "cap": inv["monthly_evaluation_cap"]}
    used, cap = evaluations_this_period(org)
    if used >= cap:
        raise HTTPException(429, "The employer's evaluation allowance is used up for now. Your upload is saved; "
                                 "contact the employer.")
    with db.tx() as c:
        db.run("update pp.submissions set status = 'queued', submitted_at = now(), heartbeat_at = now() where id = %s",
               (s["id"],), c=c)
        db.run("update pp.invites set status = 'submitted' where id = %s", (inv["id"],), c=c)
        audit.log(inv["org_id"], "candidate", _actor(inv), "submission.submitted", "submission", s["id"], c=c)
    jobs.dispatch("evaluate", s["id"])
    return redirect(f"/c/{token}#status", "Submitted. Your work is being evaluated; the employer will review the result.")


@router.post("/{token}/contest")
def contest(request: Request, token: str, message: str = Form(...)):
    inv = _invite(token)
    st = _state(inv)
    if not st["feedback"]:
        raise HTTPException(400, "You can contest a result once feedback has been released.")
    message = message.strip()
    if len(message) < 20:
        raise HTTPException(400, "Explain what you think is wrong in at least 20 characters.")
    if len([c for c in st["contests"] if c["status"] == "open"]) >= 3:
        raise HTTPException(400, "You already have open requests. The employer will respond.")
    c = db.one("insert into pp.contests (submission_id, message) values (%s, %s) returning id",
               (st["submitted"]["id"], message[:6000]))
    audit.log(inv["org_id"], "candidate", _actor(inv), "contest.opened", "submission", st["submitted"]["id"],
              {"contest": str(c["id"])})
    return redirect(f"/c/{token}#feedback", "Sent. A person at the employer will look at your request and reply here.")
