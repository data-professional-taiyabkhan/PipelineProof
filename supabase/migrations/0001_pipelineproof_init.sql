-- PipelineProof schema, version 1.
--
-- Everything lives in the "pp" schema, which is NOT exposed through the Supabase Data API.
-- The web app and the evaluation worker connect as the database owner through the connection
-- pooler. Row level security is enabled on every table with no policies, so the API roles
-- (anon, authenticated) can read nothing even if the schema were exposed by mistake.

create schema if not exists pp;

create table pp.users (
  id              uuid primary key default gen_random_uuid(),
  email           text not null unique check (email = lower(email)),
  name            text not null,
  password_hash   text not null,
  created_at      timestamptz not null default now(),
  last_login_at   timestamptz
);

create table pp.organisations (
  id                      uuid primary key default gen_random_uuid(),
  name                    text not null,
  is_demo                 boolean not null default false,
  retention_days          integer not null default 365 check (retention_days between 30 and 3650),
  monthly_evaluation_cap  integer not null default 50 check (monthly_evaluation_cap >= 0),
  created_at              timestamptz not null default now()
);

create table pp.memberships (
  org_id      uuid not null references pp.organisations(id) on delete cascade,
  user_id     uuid not null references pp.users(id) on delete cascade,
  role        text not null check (role in ('admin', 'reviewer', 'recruiter')),
  created_at  timestamptz not null default now(),
  primary key (org_id, user_id)
);
create index memberships_user_idx on pp.memberships (user_id);

-- people added to a team before they have an account; joined on sign-up with the same email
create table pp.pending_members (
  org_id      uuid not null references pp.organisations(id) on delete cascade,
  email       text not null check (email = lower(email)),
  role        text not null check (role in ('admin', 'reviewer', 'recruiter')),
  invited_by  uuid references pp.users(id) on delete set null,
  created_at  timestamptz not null default now(),
  primary key (org_id, email)
);

create table pp.sessions (
  token_hash    text primary key,                       -- sha256 of the cookie value
  user_id       uuid references pp.users(id) on delete cascade,   -- null for assessor demo sessions
  org_id        uuid references pp.organisations(id) on delete cascade,
  role          text not null,                          -- effective role in org_id ('demo' for demo sessions)
  csrf_token    text not null,
  is_demo       boolean not null default false,
  created_at    timestamptz not null default now(),
  expires_at    timestamptz not null,
  last_seen_at  timestamptz not null default now()
);
create index sessions_expiry_idx on pp.sessions (expires_at);

create table pp.tasks (
  id           text primary key,                        -- folder name under tasks/, e.g. churn_v1
  title        text not null,
  summary      text not null,
  role_family  text not null,
  metric       text not null,
  time_box     text,
  active       boolean not null default true,
  created_at   timestamptz not null default now()
);

create table pp.assessments (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references pp.organisations(id) on delete cascade,
  task_id     text not null references pp.tasks(id),
  title       text not null,
  role_title  text not null,
  deadline    timestamptz,
  status      text not null default 'open' check (status in ('open', 'closed')),
  created_by  uuid references pp.users(id) on delete set null,
  created_at  timestamptz not null default now(),
  closed_at   timestamptz
);
create index assessments_org_idx on pp.assessments (org_id, created_at desc);

create table pp.invites (
  id               uuid primary key default gen_random_uuid(),
  org_id           uuid not null references pp.organisations(id) on delete cascade,
  assessment_id    uuid not null references pp.assessments(id) on delete cascade,
  candidate_name   text not null,
  candidate_email  text,
  token            text not null unique,                 -- the candidate's private link
  status           text not null default 'invited' check (status in ('invited', 'opened', 'submitted', 'withdrawn')),
  is_synthetic     boolean not null default false,       -- demo and benchmark candidates
  consent_at       timestamptz,
  opened_at        timestamptz,
  created_by       uuid references pp.users(id) on delete set null,
  created_at       timestamptz not null default now()
);
create index invites_assessment_idx on pp.invites (assessment_id, created_at);

create table pp.submissions (
  id             uuid primary key default gen_random_uuid(),
  org_id         uuid not null references pp.organisations(id) on delete cascade,
  assessment_id  uuid not null references pp.assessments(id) on delete cascade,
  invite_id      uuid not null references pp.invites(id) on delete cascade,
  source         text not null default 'candidate' check (source in ('candidate', 'demo', 'benchmark')),
  label          text,                                   -- e.g. the benchmark submission's name
  filename       text not null,
  size_bytes     integer not null,
  sha256         text not null,
  status         text not null default 'uploaded' check (status in (
                   'uploaded', 'preflight_running', 'preflight_failed', 'preflight_passed',
                   'queued', 'running', 'scored', 'unscored', 'error')),
  preflight      jsonb,
  progress       jsonb not null default '[]'::jsonb,
  error          text,
  created_at     timestamptz not null default now(),
  submitted_at   timestamptz,
  started_at     timestamptz,
  finished_at    timestamptz,
  heartbeat_at   timestamptz
);
create index submissions_assessment_idx on pp.submissions (assessment_id, created_at desc);
create index submissions_invite_idx on pp.submissions (invite_id, created_at desc);
create index submissions_status_idx on pp.submissions (status);

create table pp.submission_files (
  submission_id  uuid primary key references pp.submissions(id) on delete cascade,
  data           bytea not null
);

create table pp.reports (
  submission_id   uuid primary key references pp.submissions(id) on delete cascade,
  report          jsonb not null,
  overall         integer,
  recommendation  text,
  rubric_version  text,
  created_at      timestamptz not null default now()
);

create table pp.reviews (
  id              uuid primary key default gen_random_uuid(),
  submission_id   uuid not null references pp.submissions(id) on delete cascade,
  reviewer_id     uuid references pp.users(id) on delete set null,
  reviewer_label  text not null,
  decision        text not null check (decision in ('confirm', 'override')),
  outcome         text not null check (outcome in ('advance', 'hold', 'reject')),
  reason          text not null check (length(reason) >= 10),
  created_at      timestamptz not null default now()
);
create index reviews_submission_idx on pp.reviews (submission_id, created_at desc);

create table pp.feedback (
  submission_id     uuid primary key references pp.submissions(id) on delete cascade,
  message           text not null,
  include_findings  boolean not null default true,
  released_by       uuid references pp.users(id) on delete set null,
  released_at       timestamptz not null default now()
);

create table pp.contests (
  id             uuid primary key default gen_random_uuid(),
  submission_id  uuid not null references pp.submissions(id) on delete cascade,
  message        text not null,
  status         text not null default 'open' check (status in ('open', 'answered')),
  response       text,
  responded_by   uuid references pp.users(id) on delete set null,
  created_at     timestamptz not null default now(),
  responded_at   timestamptz
);
create index contests_submission_idx on pp.contests (submission_id, created_at);

create table pp.audit_log (
  id           bigserial primary key,
  org_id       uuid references pp.organisations(id) on delete cascade,
  actor_type   text not null check (actor_type in ('user', 'candidate', 'system', 'demo')),
  actor_id     uuid,
  actor_label  text not null,
  action       text not null,
  entity_type  text,
  entity_id    text,
  details      jsonb not null default '{}'::jsonb,
  at           timestamptz not null default now()
);
create index audit_org_idx on pp.audit_log (org_id, at desc);

create table pp.benchmark_runs (
  id           uuid primary key default gen_random_uuid(),
  runner       text not null,
  passed       integer not null,
  total        integer not null,
  log          jsonb not null,
  started_at   timestamptz not null,
  finished_at  timestamptz not null default now()
);

-- lightweight rate limiting for sign-in, sign-up, uploads and demo runs
create table pp.rate_events (
  id    bigserial primary key,
  kind  text not null,
  key   text not null,
  at    timestamptz not null default now()
);
create index rate_events_idx on pp.rate_events (kind, key, at desc);

-- deny-all for the Data API roles
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'pp' loop
    execute format('alter table pp.%I enable row level security', t);
  end loop;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on schema pp from anon, authenticated';
    execute 'revoke all on all tables in schema pp from anon, authenticated';
    execute 'revoke all on all sequences in schema pp from anon, authenticated';
  end if;
end $$;
