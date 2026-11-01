"""Sign in, sign up, sign out, and switching organisation."""
from __future__ import annotations

import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import audit, db
from ..config import settings
from ..rendering import render
from ..security import (COOKIE, SESSION_HOURS, client_ip, create_session, destroy_session, hash_password,
                        load_ctx, verify_password)
from .common import app_ctx, read_flash, redirect

router = APIRouter()
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/app"


def _signed_in(resp: RedirectResponse, token: str) -> RedirectResponse:
    resp.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax",
                    secure=settings.cookie_secure, path="/")
    return resp


@router.get("/login")
def login_page(request: Request, next: str = "/app"):
    return render(request, "auth/login.html", ctx=load_ctx(request), next=_safe_next(next), flash=read_flash(request))


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/app")):
    email = email.strip().lower()
    if not db.rate_ok("login", f"{client_ip(request)}|{email}", 8, 900):
        raise HTTPException(429, "Too many sign-in attempts. Wait 15 minutes and try again.")
    user = db.one("select * from pp.users where email = %s", (email,))
    if not user or not verify_password(password, user["password_hash"]):
        return render(request, "auth/login.html", ctx=load_ctx(request), next=_safe_next(next), email=email,
                      error="That email and password do not match an account.", status_code=400)
    m = db.one("select org_id, role from pp.memberships where user_id = %s order by created_at limit 1", (user["id"],))
    if not m:
        return render(request, "auth/login.html", ctx=load_ctx(request), next=_safe_next(next), email=email,
                      error="Your account is not a member of any organisation any more. Ask an admin to add you again.",
                      status_code=400)
    token = create_session(user["id"], m["org_id"] if m else None, m["role"] if m else "none")
    db.run("update pp.users set last_login_at = now() where id = %s", (user["id"],))
    if m:
        audit.log(m["org_id"], "user", user["name"], "user.signed_in", "user", user["id"], {}, actor_id=user["id"])
    old = request.cookies.get(COOKIE)
    destroy_session(old)
    return _signed_in(redirect(_safe_next(next)), token)


@router.get("/signup")
def signup_page(request: Request):
    return render(request, "auth/signup.html", ctx=load_ctx(request), closed=settings.signup == "closed")


@router.post("/signup")
def signup(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
           organisation: str = Form("")):
    if settings.signup == "closed":
        raise HTTPException(403, "New accounts are by invitation only at the moment.")
    name, email, organisation = name.strip()[:120], email.strip().lower()[:200], organisation.strip()[:120]
    errors = []
    if not name:
        errors.append("Enter your name.")
    if not EMAIL.match(email):
        errors.append("Enter a valid email address.")
    if len(password) < 10:
        errors.append("Use a password of at least 10 characters.")
    pending = db.all("select org_id, role from pp.pending_members where email = %s", (email,)) if EMAIL.match(email) else []
    if not organisation and not pending:
        errors.append("Enter your organisation's name.")
    if db.one("select 1 from pp.users where email = %s", (email,)):
        errors.append("An account with that email already exists. Sign in instead.")
    if errors:
        return render(request, "auth/signup.html", ctx=load_ctx(request), errors=errors, name=name, email=email,
                      organisation=organisation, status_code=400)
    if not db.rate_ok("signup", client_ip(request), 5, 86400):
        raise HTTPException(429, "Too many new accounts from your network today.")
    with db.tx() as c:
        user = db.one("insert into pp.users (email, name, password_hash) values (%s, %s, %s) returning *",
                      (email, name, hash_password(password)), c=c)
        first_org, first_role = None, None
        for p in pending:
            db.run("insert into pp.memberships (org_id, user_id, role) values (%s, %s, %s) on conflict do nothing",
                   (p["org_id"], user["id"], p["role"]), c=c)
            audit.log(p["org_id"], "user", name, "member.added", "user", user["id"], {"role": p["role"], "via": "invitation"},
                      actor_id=user["id"], c=c)
            first_org, first_role = first_org or p["org_id"], first_role or p["role"]
        db.run("delete from pp.pending_members where email = %s", (email,), c=c)
        if organisation and not pending:
            org = db.one("insert into pp.organisations (name) values (%s) returning *", (organisation,), c=c)
            db.run("insert into pp.memberships (org_id, user_id, role) values (%s, %s, 'admin')", (org["id"], user["id"]), c=c)
            audit.log(org["id"], "user", name, "org.created", "organisation", org["id"], {"name": organisation},
                      actor_id=user["id"], c=c)
            first_org, first_role = org["id"], "admin"
    token = create_session(user["id"], first_org, first_role)
    return _signed_in(redirect("/app", f"Welcome, {name}. Your workspace is ready."), token)


@router.post("/logout")
def logout(request: Request):
    destroy_session(request.cookies.get(COOKIE))
    resp = redirect("/", "You are signed out.")
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.post("/app/switch")
def switch_org(request: Request, org_id: str = Form(...), csrf_token: str = Form("")):
    from .common import csrf
    ctx = app_ctx(request)
    csrf(ctx, csrf_token)
    if not ctx.user:
        raise HTTPException(403)
    m = db.one("select role from pp.memberships where org_id = %s and user_id = %s", (org_id, ctx.user["id"]))
    if not m:
        raise HTTPException(404)
    from ..security import sha256
    db.run("update pp.sessions set org_id = %s, role = %s where token_hash = %s",
           (org_id, m["role"], sha256(request.cookies.get(COOKIE, ""))))
    return redirect("/app")
