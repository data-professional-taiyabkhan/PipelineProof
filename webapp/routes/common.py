"""Shared helpers for route modules."""
from __future__ import annotations

from urllib.parse import quote, unquote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..config import settings
from ..security import Ctx, check_csrf, load_ctx


class LoginRequired(Exception):
    pass


class NotAllowed(Exception):
    pass


def app_ctx(request: Request) -> Ctx:
    ctx = load_ctx(request)
    if not ctx.signed_in:
        raise LoginRequired()
    return ctx


def need(ctx: Ctx, perm: str, message: str = ""):
    if not ctx.can(perm):
        raise NotAllowed(message or "Your role does not allow this action.")


def csrf(ctx: Ctx, token: str | None):
    if not check_csrf(ctx, token):
        raise HTTPException(403, "This form has expired. Go back, refresh the page and try again.")


def redirect(url: str, flash: str | None = None, kind: str = "ok") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if flash:
        resp.set_cookie("pp_flash", quote(f"{kind}|{flash}"), max_age=60, path="/", httponly=True,
                        samesite="lax", secure=settings.cookie_secure)
    return resp


def read_flash(request: Request) -> dict | None:
    raw = request.cookies.get("pp_flash")
    if not raw:
        return None
    kind, _, text = unquote(raw).partition("|")
    return {"kind": kind if kind in ("ok", "warn", "error") else "ok", "text": text[:500]}


def public_base(request: Request) -> str:
    if settings.base_url:
        return settings.base_url
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    if host and not host.startswith(("localhost", "127.0.0.1", "testserver")) and proto == "http":
        proto = "https"
    return f"{proto}://{host}"


def org_assessment(ctx: Ctx, aid: str) -> dict:
    try:
        row = db.one("""select a.*, t.title as task_title, t.metric from pp.assessments a
                        join pp.tasks t on t.id = a.task_id where a.id = %s and a.org_id = %s""", (aid, ctx.org["id"]))
    except Exception:  # noqa: BLE001 - malformed uuid
        row = None
    if not row:
        raise HTTPException(404)
    return row


def org_submission(ctx: Ctx, sid: str) -> dict:
    try:
        row = db.one("""select s.*, i.candidate_name, i.candidate_email, i.is_synthetic, i.token,
                               a.title as assessment_title, a.task_id, a.role_title, a.status as assessment_status
                        from pp.submissions s
                        join pp.invites i on i.id = s.invite_id
                        join pp.assessments a on a.id = s.assessment_id
                        where s.id = %s and s.org_id = %s""", (sid, ctx.org["id"]))
    except Exception:  # noqa: BLE001
        row = None
    if not row:
        raise HTTPException(404)
    return row


def evaluations_this_period(org: dict) -> tuple[int, int]:
    """(used, cap) for the organisation: per calendar month, or per day for the demo workspace."""
    if org.get("is_demo"):
        used = db.scalar("""select count(*) from pp.submissions where org_id = %s and source <> 'benchmark'
                            and submitted_at > now() - interval '1 day'""", (org["id"],))
        return used, settings.demo_daily_run_cap
    used = db.scalar("""select count(*) from pp.submissions where org_id = %s
                        and submitted_at >= date_trunc('month', now())""", (org["id"],))
    return used, org.get("cap") or org.get("monthly_evaluation_cap") or 0
