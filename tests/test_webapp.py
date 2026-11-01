"""End-to-end tests for the web app against a real Postgres, with the local runner.

Needs a Postgres the tests may create a database in. Set PP_TEST_PG to a libpq URI for the
server (default: postgresql://postgres@/postgres?host=/var/run/postgresql&port=5433).
Skipped when no server is reachable. Evaluations really run (local runner), so this takes a
couple of minutes.
"""
from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = os.environ.get("PP_TEST_PG", "postgresql://postgres@/postgres?host=/var/run/postgresql&port=5433")
TEST_DB = "pp_test_webapp"

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")


def _server_ok() -> bool:
    try:
        with psycopg.connect(SERVER, autocommit=True, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _server_ok(), reason="no Postgres server for web app tests")


@pytest.fixture(scope="module")
def app():
    with psycopg.connect(SERVER, autocommit=True) as c:
        c.execute(f"drop database if exists {TEST_DB} with (force)")
        c.execute(f"create database {TEST_DB}")
    url = re.sub(r"/postgres(\?|$)", f"/{TEST_DB}\\1", SERVER, count=1)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute((ROOT / "supabase" / "migrations" / "0001_pipelineproof_init.sql").read_text())
    os.environ.update({"DATABASE_URL": url, "PP_RUNNER": "local", "PP_CREATE_APP": "0", "PP_DEMO_DAILY_RUN_CAP": "5"})
    for m in [m for m in sys.modules if m == "webapp" or m.startswith("webapp.")]:
        del sys.modules[m]
    sys.path.insert(0, str(ROOT))
    from webapp import jobs, main
    jobs.set_dispatcher(lambda kind, sid: jobs.process_safely(kind, sid))   # run jobs inline
    application = main.create_app()
    yield application
    from webapp import db
    db.close()


@pytest.fixture(scope="module")
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


def zip_of(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in folder.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, f"sub/{p.relative_to(folder).as_posix()}")
    return buf.getvalue()


def signup(client, name, email, org=""):
    r = client.post("/signup", data={"name": name, "email": email, "password": "correct horse battery", "organisation": org},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return r


def test_public_pages(client):
    for path in ["/", "/how-it-works", "/evidence", "/security", "/candidates", "/demo", "/login", "/signup", "/healthz"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Content-Security-Policy" in r.headers
    assert client.get("/no-such-page").status_code == 404
    assert client.get("/app", follow_redirects=False).status_code == 303   # login required


def test_full_hiring_flow(app):
    from fastapi.testclient import TestClient
    from webapp import db
    emp = TestClient(app)
    signup(emp, "Ada Admin", "ada@example.test", "Acme Analytics")
    r = emp.get("/app")
    assert r.status_code == 200 and "Acme Analytics" in r.text
    tok = csrf_of(r.text)

    # CSRF is enforced
    assert emp.post("/app/assessments", data={"title": "x", "role_title": "y", "task_id": "churn_v1"}).status_code == 403

    r = emp.post("/app/assessments", data={"title": "ML engineer 2027", "role_title": "ML engineer", "task_id": "churn_v1",
                                           "csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    aid = r.headers["location"].rsplit("/", 1)[-1]
    r = emp.post(f"/app/assessments/{aid}/invites", data={"candidate_name": "Casey Candidate", "csrf_token": tok},
                 follow_redirects=False)
    assert r.status_code == 303
    page = emp.get(f"/app/assessments/{aid}").text
    token = re.search(r'/c/([A-Za-z0-9_-]{20,})"', page).group(1)

    cand = TestClient(app)
    r = cand.get(f"/c/{token}")
    assert r.status_code == 200 and "Hello Casey Candidate" in r.text
    assert cand.get(f"/c/{token}/train.csv").status_code == 403          # notice first
    assert cand.post(f"/c/{token}/consent", data={"agree": "1"}, follow_redirects=False).status_code == 303
    r = cand.get(f"/c/{token}/train.csv")
    assert r.status_code == 200 and r.text.startswith("row_id,customer_id")
    tz = cand.get(f"/c/{token}/starter-template.zip")
    assert tz.status_code == 200 and zipfile.ZipFile(io.BytesIO(tz.content)).namelist()

    # a broken upload is rejected with a reason
    r = cand.post(f"/c/{token}/upload", files={"file": ("x.zip", b"not a zip", "application/zip")})
    assert r.status_code == 400

    # upload the clean benchmark pipeline; the pre-flight runs inline
    data = zip_of(ROOT / "benchmark" / "submissions" / "p01_clean")
    r = cand.post(f"/c/{token}/upload", files={"file": ("p01.zip", data, "application/zip")}, follow_redirects=False)
    assert r.status_code == 303
    s = db.one("select * from pp.submissions where invite_id = (select id from pp.invites where token = %s)", (token,))
    assert s["status"] == "preflight_passed", s
    r = cand.post(f"/c/{token}/submit/{s['id']}", data={"confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303
    s = db.one("select * from pp.submissions where id = %s", (s["id"],))
    assert s["status"] == "scored", s["error"]
    rep = db.one("select * from pp.reports where submission_id = %s", (s["id"],))
    assert rep["recommendation"] == "clear" and rep["overall"] >= 90
    assert len(s["progress"]) >= 5

    # the candidate sees no result yet
    r = cand.get(f"/c/{token}")
    assert "Evaluation complete" in r.text and "Unseen customers" not in r.text
    assert cand.post(f"/c/{token}/upload", files={"file": ("p01.zip", data, "application/zip")}).status_code == 400  # locked

    # employer reviews and releases feedback
    r = emp.get(f"/app/submissions/{s['id']}")
    assert r.status_code == 200 and "No blocking findings" in r.text and "Claim against reality" in r.text
    tok = csrf_of(r.text)
    assert emp.post(f"/app/submissions/{s['id']}/feedback", data={"message": "Thanks for this work", "csrf_token": tok}).status_code == 400
    assert emp.post(f"/app/submissions/{s['id']}/review", data={"decision": "confirm", "outcome": "advance", "reason": "short",
                                                                "csrf_token": tok}).status_code == 400
    r = emp.post(f"/app/submissions/{s['id']}/review", data={"decision": "confirm", "outcome": "advance", "csrf_token": tok,
                 "reason": "Reproduced exactly and held up on unseen customers."}, follow_redirects=False)
    assert r.status_code == 303
    r = emp.post(f"/app/submissions/{s['id']}/feedback", data={"message": "Thank you, we would like to talk.",
                 "include_findings": "1", "csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303

    r = cand.get(f"/c/{token}")
    assert "Thank you, we would like to talk." in r.text and "Unseen customers" in r.text
    r = cand.post(f"/c/{token}/contest", data={"message": "I think the seed sensitivity number is off somehow."},
                  follow_redirects=False)
    assert r.status_code == 303
    c = db.one("select * from pp.contests where submission_id = %s", (s["id"],))
    r = emp.post(f"/app/contests/{c['id']}/answer", data={"response": "We checked it again; it is correct.", "csrf_token": tok},
                 follow_redirects=False)
    assert r.status_code == 303
    assert "We checked it again" in cand.get(f"/c/{token}").text

    # exports and the audit trail
    r = emp.get(f"/app/assessments/{aid}/export.csv")
    assert r.status_code == 200 and "Casey Candidate" in r.text and "advance" in r.text
    actions = {x["action"] for x in db.all("select action from pp.audit_log where org_id = (select org_id from pp.assessments where id = %s)", (aid,))}
    for a in ["org.created", "assessment.created", "invite.created", "candidate.consented", "submission.uploaded",
              "submission.preflight_passed", "submission.submitted", "evaluation.finished", "review.recorded",
              "feedback.released", "contest.opened", "contest.answered", "export.csv"]:
        assert a in actions, a
    assert emp.get(f"/app/submissions/{s['id']}/report.md").status_code == 200
    assert emp.get(f"/app/submissions/{s['id']}/files.zip").status_code == 200

    # another organisation cannot see any of it
    other = TestClient(app)
    signup(other, "Olly Other", "olly@example.test", "Other Org")
    assert other.get(f"/app/submissions/{s['id']}").status_code == 404
    assert other.get(f"/app/assessments/{aid}").status_code == 404
    assert other.get(f"/app/assessments/{aid}/export.csv").status_code == 404


def test_roles(app):
    from fastapi.testclient import TestClient
    from webapp import db
    admin = TestClient(app)
    signup(admin, "Rita Root", "rita@example.test", "Roles Ltd")
    tok = csrf_of(admin.get("/app/team").text)
    for email, role in [("rev@example.test", "reviewer"), ("rec@example.test", "recruiter")]:
        assert admin.post("/app/team", data={"email": email, "role": role, "csrf_token": tok}, follow_redirects=False).status_code == 303
    rev, rec = TestClient(app), TestClient(app)
    signup(rev, "Rev Viewer", "rev@example.test")
    signup(rec, "Rec Ruiter", "rec@example.test")
    assert "Roles Ltd" in rev.get("/app").text
    rec_tok = csrf_of(rec.get("/app").text)
    r = rec.post("/app/assessments", data={"title": "T", "role_title": "R", "task_id": "churn_v1", "csrf_token": rec_tok},
                 follow_redirects=False)
    assert r.status_code == 303
    aid = r.headers["location"].rsplit("/", 1)[-1]
    rev_tok = csrf_of(rev.get("/app").text)
    assert rev.post(f"/app/assessments/{aid}/invites", data={"candidate_name": "X", "csrf_token": rev_tok}).status_code == 403
    assert rec.get("/app/team").status_code == 200
    assert rec.post("/app/team", data={"email": "z@example.test", "role": "admin", "csrf_token": rec_tok}).status_code == 403
    # the last admin cannot be demoted
    rita = db.one("select id from pp.users where email = 'rita@example.test'")
    assert admin.post(f"/app/team/{rita['id']}/role", data={"role": "reviewer", "csrf_token": tok}).status_code == 400


def test_login_logout_and_bad_password(app):
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post("/login", data={"email": "ada@example.test", "password": "wrong password!!"})
    assert r.status_code == 400 and "do not match" in r.text
    r = c.post("/login", data={"email": "ada@example.test", "password": "correct horse battery"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert c.get("/app").status_code == 200
    c.post("/logout", follow_redirects=False)
    assert c.get("/app", follow_redirects=False).status_code == 303


def test_demo_workspace_and_live_run(app):
    from fastapi.testclient import TestClient
    from webapp import db, seed
    org, pending = seed.prepare()
    assert len(pending) == 7
    # evaluate two quickly for the demo check; mark the rest as already scored
    from webapp import jobs
    for sid in pending[:2]:
        jobs.process_safely("evaluate", sid)
    seed.finish(org)
    d = TestClient(app)
    r = d.post("/demo/enter", follow_redirects=False)
    assert r.status_code == 303
    r = d.get("/app")
    assert r.status_code == 200 and "Northwind Broadband (sample)" in r.text and "Assessor demo" in r.text
    assert d.get("/app/team").status_code == 403
    r = d.get("/app/demo-runs")
    assert r.status_code == 200 and "p13_xgboost_leak" in r.text
    tok = csrf_of(r.text)
    r = d.post("/app/demo-runs/p02_target_leak", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    sid = r.headers["location"].rsplit("/", 1)[-1]
    s = db.one("select s.status, r.report from pp.submissions s left join pp.reports r on r.submission_id = s.id where s.id = %s", (sid,))
    assert s["status"] == "scored"
    assert any(f["code"] == "LEAK_ABLATION" for f in s["report"]["findings"])
    page = d.get(f"/app/submissions/{sid}").text
    assert "Result depends on a field that is not available at prediction time" in page
    # daily cap for live demo runs (jobs not executed for this part)
    jobs.set_dispatcher(lambda kind, sid: None)
    try:
        for _ in range(5):
            d.post("/app/demo-runs/p01_clean", data={"csrf_token": tok})
        assert d.post("/app/demo-runs/p01_clean", data={"csrf_token": tok}).status_code == 429
    finally:
        jobs.set_dispatcher(lambda kind, sid: jobs.process_safely(kind, sid))


def test_maintenance(app):
    from webapp import jobs
    out = jobs.maintenance()
    assert "stale_jobs" in out and "sessions_deleted" in out
