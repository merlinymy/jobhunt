-- 001_init.sql — initial schema
-- Never edit this file after it has been applied. Add 002_*.sql instead.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================== ME ==============================

CREATE TABLE profile_facts (
  key        TEXT PRIMARY KEY,   -- work_authorization, salary_expectation, notice_period,
  value      TEXT NOT NULL,      -- phone, address, veteran_status, disability_status, ...
  updated_at TEXT NOT NULL
);

CREATE TABLE experiences (
  id          INTEGER PRIMARY KEY,
  company     TEXT NOT NULL,
  title       TEXT NOT NULL,
  start_month TEXT NOT NULL,     -- 'YYYY-MM'
  end_month   TEXT,              -- NULL = current
  location    TEXT,
  employment_type TEXT,          -- fte | contract | part_time | intern
  company_context TEXT,          -- what they did and their size AT THE TIME
  scope       TEXT,              -- team size, budget, systems, users served
  leave_reason TEXT,
  sort_order  INTEGER
);

CREATE TABLE projects (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  url        TEXT,
  blurb      TEXT,
  sort_order INTEGER
);

-- Canonical accomplishment rows. The tailor step may SELECT, REORDER, and REWORD these.
-- It may never emit a bullet with no row here. See .claude/rules/tailoring.md.
CREATE TABLE bullets (
  id            INTEGER PRIMARY KEY,
  experience_id INTEGER REFERENCES experiences(id),
  project_id    INTEGER REFERENCES projects(id),
  text          TEXT NOT NULL,
  metric        TEXT,            -- the verifiable number, if any
  skills        TEXT,            -- JSON array
  solo          INTEGER,         -- 1 = mine alone, 0 = shared credit
  sort_order    INTEGER,
  CHECK ((experience_id IS NULL) != (project_id IS NULL))
);

CREATE TABLE education (
  id          INTEGER PRIMARY KEY,
  school      TEXT NOT NULL,
  degree      TEXT,
  field       TEXT,
  start_month TEXT,
  end_month   TEXT,
  complete    INTEGER NOT NULL DEFAULT 1,
  notes       TEXT
);

CREATE TABLE credentials (           -- certifications, licenses, publications, talks
  id       INTEGER PRIMARY KEY,
  kind     TEXT NOT NULL,            -- cert | license | publication | talk | award | patent
  title    TEXT NOT NULL,
  issuer   TEXT,
  issued   TEXT,
  expires  TEXT,
  url      TEXT
);

-- Two-tier answer bank. THIS DISTINCTION IS LOAD-BEARING.
--   tier='fact'      -> stored exact, returned verbatim, NEVER generated
--   tier='narrative' -> generated once per company, then cached and reused
CREATE TABLE answers (
  id            INTEGER PRIMARY KEY,
  question_key  TEXT NOT NULL,       -- normalized: why_this_company, salary_expectation, ...
  question_text TEXT NOT NULL,       -- verbatim as seen on a real form
  tier          TEXT NOT NULL CHECK (tier IN ('fact','narrative')),
  company_id    INTEGER REFERENCES companies(id),   -- NULL = global default
  answer        TEXT NOT NULL,
  source        TEXT NOT NULL CHECK (source IN ('user','generated')),
  created_at    TEXT NOT NULL,
  UNIQUE (question_key, company_id)
);

-- STAR material. Not keyed to a form question, so it does not belong in `answers`.
-- Feeds essay fields now and the interview coach later.
CREATE TABLE stories (
  id            INTEGER PRIMARY KEY,
  title         TEXT NOT NULL,
  situation     TEXT NOT NULL,
  action        TEXT NOT NULL,
  result        TEXT NOT NULL,
  tags          TEXT,              -- JSON array: conflict, failure, ambiguity, influence, ...
  experience_id INTEGER REFERENCES experiences(id),
  created_at    TEXT NOT NULL
);

-- Questions hit on a real form with no answer available. Feeds the chat intake loop.
CREATE TABLE unknown_questions (
  id             INTEGER PRIMARY KEY,
  question_text  TEXT NOT NULL,
  application_id INTEGER REFERENCES applications(id),
  seen_count     INTEGER NOT NULL DEFAULT 1,
  resolved_by    INTEGER REFERENCES answers(id),
  created_at     TEXT NOT NULL
);

-- ============================ MARKET ============================

CREATE TABLE companies (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  name_norm   TEXT NOT NULL UNIQUE,
  ats_type    TEXT,               -- DERIVED from apply_url regex; not hand-maintained
  ats_slug    TEXT,
  careers_url TEXT,
  blocked     INTEGER NOT NULL DEFAULT 0,
  notes       TEXT
);

CREATE TABLE jobs (
  id             INTEGER PRIMARY KEY,
  company_id     INTEGER NOT NULL REFERENCES companies(id),
  title          TEXT NOT NULL,
  title_norm     TEXT NOT NULL,
  location       TEXT,
  remote         TEXT,            -- onsite | hybrid | remote | unknown
  apply_url      TEXT NOT NULL,
  apply_url_norm TEXT NOT NULL UNIQUE,   -- hard dedup key
  jd_text        TEXT,
  comp_min       INTEGER,
  comp_max       INTEGER,
  source         TEXT NOT NULL,   -- 'jobspy:indeed' | 'jobspy:google' | 'greenhouse:direct'
  posted_at      TEXT,
  discovered_at  TEXT NOT NULL
);

-- Soft dedup: same role reposted, or listed under a second URL by another aggregator.
CREATE INDEX idx_jobs_softdedup ON jobs (company_id, title_norm, location);

-- =========================== PIPELINE ===========================

CREATE TABLE applications (
  id                  INTEGER PRIMARY KEY,
  job_id              INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
  state               TEXT NOT NULL CHECK (state IN (
                        'discovered','scored','filtered','job_approved','skipped',
                        'packet_ready','expired','applied','rejected','interview','offer')),
  score               REAL,
  score_reasoning     TEXT,
  resume_pdf          BLOB,       -- EXACT bytes submitted
  resume_data         TEXT,       -- the RenderCV input that produced them
  answers_json        TEXT,       -- EXACT answer set submitted
  referral_contact_id INTEGER REFERENCES contacts(id),
  would_apply_anyway  INTEGER,    -- honesty flag; drives the drift metric
  applied_at          TEXT,
  first_response_at   TEXT,
  outcome             TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

CREATE INDEX idx_applications_state ON applications (state);

CREATE TABLE events (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES applications(id),
  kind           TEXT NOT NULL CHECK (kind IN
                   ('state_change','email_in','note','interview','digest_sent')),
  from_state     TEXT,
  to_state       TEXT,
  detail         TEXT,
  email_msg_id   TEXT,
  occurred_at    TEXT NOT NULL
);

-- Idempotency guard: one digest notification per application, ever.
CREATE UNIQUE INDEX idx_events_digest_once
  ON events (application_id, kind) WHERE kind = 'digest_sent';

CREATE UNIQUE INDEX idx_events_email_once
  ON events (email_msg_id) WHERE email_msg_id IS NOT NULL;

-- =========================== NETWORK ============================

CREATE TABLE contacts (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  company_id     INTEGER REFERENCES companies(id),
  relationship   TEXT,            -- former_coworker | alum | friend | weak
  channel        TEXT,
  handle         TEXT,
  do_not_contact INTEGER NOT NULL DEFAULT 0,
  last_pinged_at TEXT,
  notes          TEXT
);

-- ========================= OBSERVABILITY ========================

CREATE TABLE llm_calls (
  id             INTEGER PRIMARY KEY,
  task           TEXT NOT NULL,   -- tailor | narrative_answer | score | classify_email
  model          TEXT NOT NULL,
  application_id INTEGER REFERENCES applications(id),
  prompt         TEXT NOT NULL,
  response       TEXT,
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  cost_usd       REAL,
  latency_ms     INTEGER,
  error          TEXT,
  called_at      TEXT NOT NULL
);
