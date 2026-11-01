"""Public pages: product, how it works, evidence, security, assessor demo entry."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from .. import audit, db
from ..config import settings
from ..rendering import render
from ..security import COOKIE, DEMO_SESSION_HOURS, client_ip, create_session, load_ctx
from .common import read_flash, redirect

router = APIRouter()


def latest_benchmark() -> dict | None:
    try:
        row = db.one("select * from pp.benchmark_runs order by finished_at desc limit 1")
    except Exception:  # noqa: BLE001
        row = None
    if row:
        return {"date": row["finished_at"], "runner": row["runner"], "passed": row["passed"], "total": row["total"],
                "log": row["log"]}
    logs = sorted((settings.benchmark_dir / "logs").glob("evaluation_log_*.json")) if \
        (settings.benchmark_dir / "logs").exists() else []
    if logs:
        log = json.loads(logs[-1].read_text())
        return {"date": log["date"], "runner": log["runner"], "passed": log["passed"], "total": log["total"], "log": log}
    return None


def demo_org() -> dict | None:
    try:
        return db.one("select * from pp.organisations where is_demo order by created_at limit 1")
    except Exception:  # noqa: BLE001
        return None


def page(request: Request, name: str, **data):
    return render(request, name, ctx=load_ctx(request), flash=read_flash(request), **data)


@router.get("/")
def landing(request: Request):
    return page(request, "public/landing.html", bench=latest_benchmark(), active="product")


@router.get("/how-it-works")
def how(request: Request):
    return page(request, "public/how.html", active="how")


@router.get("/evidence")
def evidence(request: Request):
    history = []
    try:
        history = db.all("select runner, passed, total, finished_at from pp.benchmark_runs order by finished_at desc limit 10")
    except Exception:  # noqa: BLE001
        pass
    from pipelineproof.benchmark import PLANTED
    return page(request, "public/evidence.html", bench=latest_benchmark(), history=history, planted=PLANTED,
                active="evidence")


@router.get("/security")
def security_page(request: Request):
    import os
    region = routing = None
    if settings.runner == "modal":
        code = os.environ.get("PP_MODAL_REGION", "uk")
        region = {"uk": "UK", "eu": "EU", "us": "US"}.get(code, code) if code else None
        r = os.environ.get("PP_MODAL_ROUTING", "")
        routing = {"eu-west": "Dublin, Ireland", "us-east": "Virginia, USA", "us-west": "the western USA",
                   "ap-south": "Mumbai, India"}.get(r or "us-east", r)
    return page(request, "public/security.html", region=region, routing=routing, active="security")


@router.get("/candidates")
def for_candidates(request: Request):
    return page(request, "public/candidates.html", active="candidates")


@router.get("/demo")
def demo(request: Request):
    org = demo_org()
    stats = None
    if org:
        stats = db.one("""select count(*) filter (where s.source = 'benchmark') as sample_reports,
                                 (select count(*) from pp.invites i where i.org_id = %s and i.is_synthetic) as candidates
                          from pp.submissions s where s.org_id = %s""", (org["id"], org["id"]))
    return page(request, "public/demo.html", org=org, stats=stats, active="demo")


@router.post("/demo/enter")
def demo_enter(request: Request):
    org = demo_org()
    if not org:
        raise HTTPException(503, "The assessor demo has not been set up yet.")
    if not db.rate_ok("demo_enter", client_ip(request), 30, 3600):
        raise HTTPException(429, "Too many demo sessions from your network in the last hour. Try again later.")
    token = create_session(None, org["id"], "demo", is_demo=True)
    audit.log(org["id"], "demo", "Demo assessor", "demo.entered", "organisation", org["id"], {})
    resp = redirect("/app", "You are in the assessor demo. Everything here is synthetic.")
    resp.set_cookie(COOKIE, token, max_age=DEMO_SESSION_HOURS * 3600, httponly=True, samesite="lax",
                    secure=settings.cookie_secure, path="/")
    return resp
