# Deploying PipelineProof

Production runs on two services:

- **Supabase** (Postgres, London, eu-west-2): project `pipelineproof`, ref `olropxhrtvydfouwnzre`.
- **Modal**: app `pipelineproof` (web app, worker, daily maintenance) and app
  `pipelineproof-sandboxes` (where candidate code runs).

The only secret is the database connection string, kept in a Modal secret called `pipelineproof-db`.

## One-time setup

1. **Database schema.** Already applied to the Supabase project. For a new database, run
   `supabase/migrations/0001_pipelineproof_init.sql` once (Supabase SQL editor, or `psql`).
2. **Database password.** Supabase → project → Project Settings → Database → *Reset database password*
   → *Generate a password* (letters and numbers only).
3. **`.env` file** in the repository root (already ignored by Git):
   ```
   DATABASE_URL=postgresql://postgres.olropxhrtvydfouwnzre:<password>@aws-0-eu-west-2.pooler.supabase.com:5432/postgres
   ```
   Use the host shown under *Connect → Session pooler* in Supabase if it differs.
4. **Modal login**: `modal token new` (opens the browser; approve once per computer).
5. **Modal secret** from the `.env` file:
   ```
   modal secret create pipelineproof-db --from-dotenv .env --force
   ```

## Deploy and update

```
modal deploy modal_app.py
```

The live address is **https://data-professional-taiyabkhan--pipelineproof.eu-west.modal.run**.
Run the same command after every change. New requests go to the new version; a container that is
already warm can keep serving the old code for a while, so after a deploy that changes behaviour, stop
it with `modal container list` and `modal container stop -y <id>` (the next request starts a fresh one).

Web requests enter Modal's network through its Dublin edge (`routing_region="eu-west"` on the `site`
function); the containers and sandboxes run in Modal's UK region; the database is in London. The
routing region can only be set when a function is first created, so changing it means renaming the
function (and the address changes).

Deploy-time options are environment variables read when you run the command, for example in PowerShell:

```
$env:PP_KEEP_WARM = "1"; modal deploy modal_app.py      # no cold start (keeps one small container running)
$env:PP_SIGNUP = "closed"; modal deploy modal_app.py    # only people added to a team can sign up
```

## Assessor demo and evidence

```
modal run modal_app.py::seed          # sample workspace, 7 synthetic candidates, real evaluations in sandboxes
modal run modal_app.py::seed --reset  # rebuild it from scratch
modal run modal_app.py::benchmark     # all 18 benchmark submissions through production sandboxes; shown on /evidence
```

## Custom domain

Modal's Starter plan serves the app at a `modal.run` address. A custom domain such as
`app.pipelineproof.co.uk` needs Modal's Team plan, or a reverse proxy in front (for example a
Cloudflare Worker), or hosting the web app elsewhere while keeping the worker on Modal.

## Costs at pilot scale

- **Modal** Starter plan: free, with $30 of compute credit each month. An evaluation uses about
  ten short sandbox runs (around 2 to 4 US cents); the web container costs nothing while idle unless
  `PP_KEEP_WARM=1` (a few dollars a month).
- **Supabase** free plan: the project pauses after a week without activity, which would take the site
  down until someone restores it in the dashboard. The daily maintenance job connects to the database
  every day, which may be enough to keep it active, but do not rely on that: upgrade to Pro ($25 a month)
  before real pilots or an assessment window.

## Checks after a deploy

- `/healthz` returns `{"ok": true, "db": true}`.
- `python scripts/live_candidate_check.py <address>` runs the whole candidate journey in the demo
  (invite, notice, upload, pre-flight, submit, evaluation) and prints `LIVE CANDIDATE CHECK OK`.
- `/demo` → *Enter the demo workspace* shows seven scored candidates.
- *Live benchmark runs* → *Run live* on `p02_target_leak` finishes with a critical leakage finding.
- `/evidence` shows the latest benchmark run.
