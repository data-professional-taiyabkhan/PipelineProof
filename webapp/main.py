"""FastAPI application factory.

    uvicorn webapp.main:app --reload          # local development (uses the local runner)

On Modal, modal_app.py calls create_app() and installs a dispatcher that spawns the
evaluation worker in its own container.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, jobs, tasks_registry
from .rendering import render
from .routes import auth, candidate, employer, public
from .routes.common import LoginRequired, NotAllowed

log = logging.getLogger("pipelineproof")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def create_app(dispatch=None) -> FastAPI:
    if dispatch is not None:
        jobs.set_dispatcher(dispatch)
    @asynccontextmanager
    async def lifespan(_app):
        try:
            tasks_registry.sync_to_db()
        except Exception:  # noqa: BLE001 - the site should still serve static pages
            log.exception("could not sync tasks to the database")
        yield

    app = FastAPI(title="PipelineProof", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    for r in (public.router, auth.router, employer.router, candidate.router):
        app.include_router(r)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith(("/app", "/c/", "/login", "/signup")):
            response.headers.setdefault("Cache-Control", "no-store")
        if "pp_flash" in request.cookies and response.status_code == 200 and \
                response.headers.get("content-type", "").startswith("text/html") and \
                not request.headers.get("hx-request"):
            response.delete_cookie("pp_flash", path="/")
        return response

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    @app.exception_handler(NotAllowed)
    async def _forbidden(request: Request, exc: NotAllowed):
        return render(request, "error.html", status_code=403, title="Not allowed",
                      message=str(exc) or "Your role does not allow this action.")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/") or request.headers.get("hx-request"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        titles = {404: "Page not found", 403: "Not allowed", 413: "File too large", 429: "Too many requests"}
        return render(request, "error.html", status_code=exc.status_code,
                      title=titles.get(exc.status_code, "Something went wrong"),
                      message=exc.detail if isinstance(exc.detail, str) and exc.status_code != 404
                      else "We could not find that page.")

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return render(request, "error.html", status_code=500, title="Something went wrong",
                      message="An unexpected error occurred. It has been logged.")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "db": db.ping()}

    _ = FastAPIHTTPException
    return app


app = None
if __name__ != "__main__":
    import os
    if os.environ.get("PP_CREATE_APP", "1") == "1":
        app = create_app()
