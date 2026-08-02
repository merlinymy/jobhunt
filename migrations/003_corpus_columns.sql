-- Two unrelated needs, as scoped in docs/build-plan.md Phase 2.
--
-- 1. Columns the corpus already carries that 001_init.sql has no home for. The
--    YAML leads the schema deliberately — docs/profile/ is the source of truth
--    and the DB is derived — so the loader cannot round-trip experience.yaml
--    without these. Every one is additive: ALTER TABLE ADD COLUMN, no rebuild.
--
-- 2. The `answers` global-default uniqueness gap. `UNIQUE (question_key,
--    company_id)` reads as if it covers everything, but SQLite treats NULLs as
--    distinct and `company_id IS NULL` is how a global default is encoded, so
--    two global answers to one question both insert. Verified during the Phase 1
--    audit. Unreachable then (nothing wrote `answers`); load-bearing in Phase 3,
--    where resolution falls back to `company_id IS NULL` with no ORDER BY and a
--    fact-tier answer is returned verbatim.
--
-- Multi-valued YAML fields are stored as JSON arrays in TEXT, matching the
-- existing `bullets.skills` convention rather than inventing join tables for
-- lists that are only ever read whole.

-- ------------------------------ experiences ------------------------------
ALTER TABLE experiences ADD COLUMN titles_history   TEXT;  -- JSON array, oldest first
ALTER TABLE experiences ADD COLUMN known_for        TEXT;
ALTER TABLE experiences ADD COLUMN recognition      TEXT;  -- JSON array
ALTER TABLE experiences ADD COLUMN tech_built       TEXT;  -- JSON array
ALTER TABLE experiences ADD COLUMN tech_owned       TEXT;  -- JSON array
ALTER TABLE experiences ADD COLUMN tech_maintained  TEXT;  -- JSON array
ALTER TABLE experiences ADD COLUMN tech_touched     TEXT;  -- JSON array

-- -------------------------------- projects --------------------------------
ALTER TABLE projects ADD COLUMN role            TEXT;
ALTER TABLE projects ADD COLUMN traction        TEXT;
ALTER TABLE projects ADD COLUMN scope           TEXT;
ALTER TABLE projects ADD COLUMN start_month     TEXT;      -- 'YYYY-MM'
ALTER TABLE projects ADD COLUMN end_month       TEXT;      -- NULL = ongoing
ALTER TABLE projects ADD COLUMN tech_built      TEXT;      -- JSON array
ALTER TABLE projects ADD COLUMN tech_owned      TEXT;      -- JSON array
ALTER TABLE projects ADD COLUMN tech_maintained TEXT;      -- JSON array
ALTER TABLE projects ADD COLUMN tech_touched    TEXT;      -- JSON array

-- -------------------------------- languages --------------------------------
CREATE TABLE languages (
  id       INTEGER PRIMARY KEY,
  language TEXT NOT NULL UNIQUE,
  fluency  TEXT
);

-- --------------------- natural keys, so the loader upserts ---------------------
-- `make load-profile` re-runs on every edit to docs/profile/. Without a unique
-- key per row it would either duplicate the corpus or need a delete-and-reload,
-- and reloading would churn `bullets.id` — the IDs the tailor selects by.
CREATE UNIQUE INDEX idx_experiences_natural ON experiences (company, title, start_month);
CREATE UNIQUE INDEX idx_projects_natural    ON projects (name);
CREATE UNIQUE INDEX idx_education_natural   ON education (school, degree, field);
CREATE UNIQUE INDEX idx_credentials_natural ON credentials (kind, title, issuer);

-- A bullet is identified by its position under its parent, so editing the text
-- of bullet 3 updates row 3 rather than orphaning it and appending a new one.
CREATE UNIQUE INDEX idx_bullets_experience_order
  ON bullets (experience_id, sort_order) WHERE experience_id IS NOT NULL;
CREATE UNIQUE INDEX idx_bullets_project_order
  ON bullets (project_id, sort_order) WHERE project_id IS NOT NULL;

-- ------------------------- answers global default -------------------------
CREATE UNIQUE INDEX idx_answers_global_once
  ON answers (question_key) WHERE company_id IS NULL;
