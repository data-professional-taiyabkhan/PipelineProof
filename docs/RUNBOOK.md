# Runbook

## Where to look

| Question | Where |
|---|---|
| Is the site up and connected to the database? | `https://<app>/healthz` |
| Web or worker errors | Modal dashboard → app `pipelineproof` → function `web` or `run_job` → Logs (kept 1 day on Starter) |
| Sandbox activity | Modal dashboard → app `pipelineproof-sandboxes` |
| Database load, size, pause state | Supabase dashboard → project `pipelineproof` |
| What a team did | In the app: *Audit log* |

## Common situations

**An evaluation is stuck on "Evaluating".** The worker sends a heartbeat every 20 seconds. The daily job
marks jobs without a heartbeat for 45 minutes as errors. To retry sooner, an admin opens the submission
and clicks *Re-run*. If many are stuck, check `run_job` logs in Modal.

**A candidate's pre-flight keeps failing with "could not run".** That message means an infrastructure
error, not a problem in their code. Check `run_job` logs. The candidate can upload again; nothing is lost.

**"Requirements could not be installed" (ENV_BUILD).** The candidate pinned versions that do not exist
or have no wheel for their Python version. It is reported to the reviewer as a finding; the candidate's
pre-flight would have shown it too.

**The site shows a database error.** Check whether the Supabase project is paused (free plan, after a
week idle) and restore it from the dashboard. If the password was changed, update the secret:
`modal secret create pipelineproof-db --from-dotenv .env --force`, then `modal deploy modal_app.py`.

**The assessor demo looks messy.** Visitors' live runs, uploads and decisions are cleared daily. To
reset now: `modal run modal_app.py::seed --reset`.

**Someone needs access to an organisation.** An admin adds their email under *Team*. If they already
have an account they get access at once; otherwise they join when they sign up with that email.

**Rotate the database password.** Reset it in Supabase, update `.env`, run the `modal secret create
... --force` command above, then `modal deploy modal_app.py`.

## Data retention

Each organisation has a retention period (365 days by default). The daily job deletes the candidates,
submissions, reports, reviews and feedback of assessments closed for longer than that. The audit log
records each deletion.

## Limits that protect costs

- Live demo evaluations: 40 per day across all visitors (`PP_DEMO_DAILY_RUN_CAP`), 6 per hour per network.
- Organisations: 50 evaluations per month by default (`organisations.monthly_evaluation_cap`).
- Uploads: 20 MB zip, 60 MB unpacked, 500 files; 15 uploads per candidate per hour.
- Sign-in: 8 attempts per 15 minutes per network and email. Sign-up: 5 per day per network.
