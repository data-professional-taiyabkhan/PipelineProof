"""PipelineProof on Modal: web app, evaluation worker, daily maintenance, demo seed, benchmark.

    modal deploy modal_app.py               # the web app, the worker and the daily job
    modal run modal_app.py::seed            # build the assessor demo (runs real evaluations)
    modal run modal_app.py::benchmark       # run the planted-defect benchmark, store it for /evidence
    modal run modal_app.py::maintenance     # run the daily housekeeping now

Needs a Modal secret called "pipelineproof-db" holding DATABASE_URL, the Supabase session-pooler
connection string. See docs/DEPLOY.md.

Where things run:
  site         FastAPI app (pages, API, candidate uploads); holds no candidate code
  run_job      one container per evaluation; it orchestrates, scores the holdout, and starts the
               sandboxes, but never executes candidate code itself
  sandboxes    created by run_job through ModalRunner: gVisor, block_network=True, CPU / memory /
               time caps, one per run, destroyed afterwards
"""
from __future__ import annotations

import os

import modal

REGION = os.environ.get("PP_MODAL_REGION", "uk")          # set PP_MODAL_REGION="" to let Modal choose
# Where web requests enter Modal's network. Modal's default edge is us-east (Virginia); eu-west is Dublin.
# It can only be set when a function is first created, so changing it means renaming the function.
ROUTING = os.environ.get("PP_MODAL_ROUTING", "eu-west")
KEEP_WARM = int(os.environ.get("PP_KEEP_WARM", "0"))      # 1 keeps one web container always running
_region = {"region": REGION} if REGION else {}
_ignore = ["**/__pycache__", "**/*.pyc", "**/.ipynb_checkpoints", "**/.DS_Store"]

app = modal.App("pipelineproof")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements("requirements-app.txt")
    .env({
        "PYTHONPATH": "/root/pp", "PP_ROOT": "/root/pp", "PP_ENV": "modal", "PP_RUNNER": "modal",
        "PP_COOKIE_SECURE": "1", "PP_CREATE_APP": "0", "PP_MODAL_REGION": REGION,
        "PP_SIGNUP": os.environ.get("PP_SIGNUP", "open"),
        "PP_CONTACT_EMAIL": os.environ.get("PP_CONTACT_EMAIL", ""),
        "PP_DEMO_DAILY_RUN_CAP": os.environ.get("PP_DEMO_DAILY_RUN_CAP", "40"),
        "PP_MODAL_ROUTING": ROUTING,
    })
    .add_local_dir("pipelineproof", "/root/pp/pipelineproof", ignore=_ignore)
    .add_local_dir("webapp", "/root/pp/webapp", ignore=_ignore)
    .add_local_dir("tasks", "/root/pp/tasks", ignore=_ignore)
    .add_local_dir("benchmark/submissions", "/root/pp/benchmark/submissions", ignore=_ignore)
    .add_local_dir("benchmark/logs", "/root/pp/benchmark/logs", ignore=_ignore)
)
secrets = [modal.Secret.from_name("pipelineproof-db")]


@app.function(image=image, secrets=secrets, timeout=3600, max_containers=10, cpu=1.0, memory=2048, **_region)
def run_job(kind: str, submission_id: str) -> bool:
    """Run one pre-flight check or evaluation; results are written to the database."""
    from webapp import jobs
    return jobs.process_safely(kind, submission_id) is not None


def _dispatch(kind: str, submission_id: str):
    run_job.spawn(kind, submission_id)


@app.function(image=image, secrets=secrets, timeout=300, max_containers=3, cpu=0.5, memory=1024,
              scaledown_window=1200, min_containers=KEEP_WARM, **_region,
              **({"routing_region": ROUTING} if ROUTING else {}))
@modal.concurrent(max_inputs=40)
@modal.asgi_app(label="pipelineproof")
def site():
    from webapp.main import create_app
    return create_app(dispatch=_dispatch)


@app.function(image=image, secrets=secrets, timeout=900, schedule=modal.Cron("17 3 * * *"), **_region)
def maintenance():
    """Daily: stop stuck jobs, delete data past retention, clear the demo's visitor activity."""
    from webapp import jobs
    out = jobs.maintenance()
    print(out)
    return out


@app.function(image=image, secrets=secrets, timeout=3600, **_region)
def seed(reset: bool = False):
    """Create the assessor demo workspace and evaluate its sample submissions in parallel."""
    from webapp import seed as demo
    org, pending = demo.prepare(reset)
    print(f"{len(pending)} sample submissions to evaluate")
    results = list(run_job.map(["evaluate"] * len(pending), pending, return_exceptions=True))
    demo.finish(org)
    print(f"done: {sum(1 for r in results if r is True)} of {len(pending)} evaluated")


@app.function(image=image, secrets=secrets, timeout=3600, **_region)
def benchmark():
    """Run all 18 planted-defect submissions through the production sandboxes and store the log."""
    from webapp import seed as demo
    log = demo.run_benchmark_local()
    print(f"benchmark {log['passed']}/{log['total']} in {log['seconds']}s")
    return {"passed": log["passed"], "total": log["total"]}
