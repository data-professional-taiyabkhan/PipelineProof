# Architecture

```
 Browser (employer, reviewer, candidate, assessor)
        │  HTTPS
        ▼
 ┌──────────────────────────────┐        ┌──────────────────────────────────┐
 │ Modal function "site"        │ spawn  │ Modal function "run_job"         │
 │ FastAPI + Jinja + HTMX       │ ─────► │ one container per job            │
 │ pages, uploads, reviews      │        │ pre-flight or full evaluation    │
 │ never runs candidate code    │        │ scores the holdout (labels here) │
 └──────────────┬───────────────┘        └───────┬──────────────┬───────────┘
                │ Postgres (TLS, pooler)          │              │ Sandbox.create, per run
                ▼                                 ▼              ▼
 ┌──────────────────────────────┐        ┌──────────────────────────────────┐
 │ Supabase Postgres, London    │ ◄───── │ Modal Sandboxes (gVisor)         │
 │ schema "pp", not exposed via │ results│ block_network=True, CPU/memory/  │
 │ the Data API, RLS deny-all   │        │ time caps, destroyed after a run │
 └──────────────────────────────┘        └──────────────────────────────────┘
```

Web traffic enters through Modal's Dublin edge (`routing_region="eu-west"`); containers and sandboxes
run in Modal's UK region; Postgres is in London. A daily Modal cron (`maintenance`) stops stuck jobs, deletes data past retention, and clears the
assessor demo's visitor activity.

## Components

| Part | Code | Notes |
|---|---|---|
| Web app | `webapp/` | FastAPI, server-rendered Jinja templates, HTMX for live progress. Sessions in Postgres, Argon2id passwords, CSRF token on every form, strict CSP, no third-party assets. |
| Worker | `webapp/jobs.py`, `modal_app.py::run_job` | Loads the zip from Postgres, extracts it safely, runs `pipeline.preflight` or `pipeline.evaluate`, writes progress, report and audit entries. Heartbeat every 20 s. |
| Harness | `pipelineproof/` | Contract, static rules, runs, trace analysis, ablation, scoring. Independent of the web app; also has a CLI. |
| Sandboxes | `pipelineproof/runners/modal_runner.py` | Image built from the candidate's pins (network on, no data). Each run: fresh sandbox, submission + instrumentation + inputs copied in as tar, command run with a timeout, `/out` streamed back as a gzipped tar, sandbox terminated. |
| Database | `supabase/migrations/` | Postgres 17 on Supabase, eu-west-2. Submission zips stored in `pp.submission_files` (bytea, 20 MB cap). |

## Data model (schema `pp`)

`organisations` → `memberships` (admin, reviewer, recruiter) ← `users`; `pending_members` for people
added before they sign up. `assessments` (task + role + deadline) → `invites` (candidate, private link
token) → `submissions` (status machine below) → `reports` (JSON report), `reviews` (decision, outcome,
reason), `feedback` (released message), `contests` (candidate requests and answers). `audit_log` records
every action. `benchmark_runs` stores evaluation logs for the evidence page. `sessions` and
`rate_events` support sign-in and rate limits. `tasks` mirrors the folders under `tasks/`.

Submission status: `uploaded → preflight_running → preflight_failed | preflight_passed → queued →
running → scored | unscored | error`.

## Request flows

**Candidate:** opens `/c/<token>` → accepts the notice → downloads `train.csv` and the starter template
→ uploads a zip (validated: size, file count, paths) → the worker runs the pre-flight → the candidate
submits a passed upload → the worker evaluates → a reviewer decides and may release feedback → the
candidate reads it and may contest.

**Employer:** signs up (creates an organisation) or joins one → creates an assessment from the task
library → invites candidates (copy the private link) → watches progress → reads reports → records a
decision with a reason → releases feedback → answers contests → exports CSV.

**Assessor demo:** `/demo` → a session in the sample organisation (no account) → seeded reports for
seven synthetic candidates → live runs of the 16 benchmark submissions (daily cap).

## Security rules

1. Holdout labels never enter a sandbox; predictions are scored in the worker.
2. The environment build has network but no data; runs have data but no network.
3. No secrets in sandboxes; each sandbox is destroyed after its run.
4. Telemetry from inside a sandbox is untrusted; a missing trace is itself a finding.
5. The web container never executes candidate code.
6. Every organisation-scoped query filters by the session's organisation; other organisations' records
   return 404.

## Configuration

| Variable | Where | Meaning |
|---|---|---|
| `DATABASE_URL` | Modal secret `pipelineproof-db`, or `.env` locally | Postgres connection string (Supabase session pooler) |
| `PP_RUNNER` | image env | `modal` in production; `local` or `docker` for development |
| `PP_MODAL_REGION` | deploy-time env | sandbox and function region, default `uk`; empty lets Modal choose |
| `PP_MODAL_ROUTING` | deploy-time env | where web requests enter Modal, default `eu-west` (Dublin); fixed when the function is created |
| `PP_SIGNUP` | deploy-time env | `open` (default) or `closed` (team invitations only) |
| `PP_KEEP_WARM` | deploy-time env | `1` keeps one web container running (no cold start, costs a little) |
| `PP_DEMO_DAILY_RUN_CAP` | deploy-time env | live demo evaluations per day, default 40 |
| `PP_CONTACT_EMAIL` | deploy-time env | shown in the footer when set |
