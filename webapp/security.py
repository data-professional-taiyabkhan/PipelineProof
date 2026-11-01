"""Passwords, server-side sessions, CSRF tokens and request context."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request

from . import db
from .config import settings

COOKIE = "pp_session"
SESSION_HOURS = 24 * 7
DEMO_SESSION_HOURS = 6
_ph = PasswordHasher()

ROLE_LABELS = {"admin": "Admin", "reviewer": "Reviewer", "recruiter": "Recruiter", "demo": "Demo assessor"}
# what each role may do inside an organisation
PERMISSIONS = {
    "admin": {"view", "invite", "assess", "review", "release", "team", "export", "rerun", "settings"},
    "reviewer": {"view", "review", "release", "export"},
    "recruiter": {"view", "invite", "assess", "export"},
    "demo": {"view", "invite", "assess", "review", "release", "export", "demo_run"},
}


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def sign(value: str) -> str:
    return hmac.new(settings.app_secret(), value.encode(), hashlib.sha256).hexdigest()[:32]


@dataclass
class Ctx:
    """Who is making this request."""
    user: dict | None = None
    org: dict | None = None
    role: str | None = None
    is_demo: bool = False
    csrf: str = ""
    orgs: list | None = None

    @property
    def signed_in(self) -> bool:
        return self.org is not None and (self.user is not None or self.is_demo)

    def can(self, perm: str) -> bool:
        return bool(self.role) and perm in PERMISSIONS.get(self.role, set())

    @property
    def label(self) -> str:
        if self.user:
            return self.user["name"]
        return "Demo assessor" if self.is_demo else "Anonymous"

    @property
    def actor_type(self) -> str:
        return "demo" if self.is_demo else "user"

    @property
    def actor_id(self):
        return self.user["id"] if self.user else None


def create_session(user_id, org_id, role: str, is_demo: bool = False) -> str:
    token = new_token()
    hours = DEMO_SESSION_HOURS if is_demo else SESSION_HOURS
    db.run("""insert into pp.sessions (token_hash, user_id, org_id, role, csrf_token, is_demo, expires_at)
              values (%s, %s, %s, %s, %s, %s, now() + make_interval(hours => %s))""",
           (sha256(token), user_id, org_id, role, new_token(24), is_demo, hours))
    return token


def destroy_session(token: str | None):
    if token:
        db.run("delete from pp.sessions where token_hash = %s", (sha256(token),))


def load_ctx(request: Request) -> Ctx:
    cached = getattr(request.state, "ctx", None)
    if cached is not None:
        return cached
    ctx = Ctx()
    token = request.cookies.get(COOKIE)
    if token:
        row = db.one("""select s.*, o.name as org_name, o.is_demo as org_is_demo, o.retention_days,
                               o.monthly_evaluation_cap,
                               u.id as u_id, u.name as u_name, u.email as u_email
                        from pp.sessions s
                        left join pp.organisations o on o.id = s.org_id
                        left join pp.users u on u.id = s.user_id
                        where s.token_hash = %s and s.expires_at > now()""", (sha256(token),))
        if row:
            ctx.csrf = row["csrf_token"]
            ctx.is_demo = row["is_demo"]
            if row["u_id"]:
                ctx.user = {"id": row["u_id"], "name": row["u_name"], "email": row["u_email"]}
            if row["org_id"]:
                ctx.org = {"id": row["org_id"], "name": row["org_name"], "is_demo": row["org_is_demo"],
                           "retention_days": row["retention_days"], "cap": row["monthly_evaluation_cap"]}
                ctx.role = row["role"]
                if ctx.user:   # role may have changed since sign-in
                    m = db.one("select role from pp.memberships where org_id = %s and user_id = %s",
                               (row["org_id"], row["u_id"]))
                    ctx.role = m["role"] if m else None
                    if not m:
                        ctx.org = None
            if ctx.user:
                ctx.orgs = db.all("""select o.id, o.name, m.role from pp.memberships m
                                     join pp.organisations o on o.id = m.org_id
                                     where m.user_id = %s order by o.name""", (ctx.user["id"],))
            if row["last_seen_at"] < datetime.now(timezone.utc) - timedelta(minutes=10):
                db.run("update pp.sessions set last_seen_at = now() where token_hash = %s", (sha256(token),))
    request.state.ctx = ctx
    return ctx


def check_csrf(ctx: Ctx, submitted: str | None) -> bool:
    return bool(ctx.csrf) and bool(submitted) and hmac.compare_digest(ctx.csrf, submitted)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown"))[:64]
