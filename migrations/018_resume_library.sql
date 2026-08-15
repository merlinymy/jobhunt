-- The resume library: the select-from-a-library engine's source tables.
--
-- Phase 1 of the resume-generation redesign (docs/resume-redesign.md). The
-- tailor stops rewording a raw corpus and starts selecting from these
-- human-approved, guard-annotated rows. Loaded from the untracked
-- docs/profile/resume_library.yaml by `load_profile._load_library`.
--
-- Additive, as the phase requires: the old corpus tables (experiences,
-- projects, bullets, education, credentials, languages) are left in place and
-- untouched. Nothing that still reads them is repointed in this migration — the
-- readers move over in later Phase 1 checkpoints, and the old tables are dropped
-- only once nothing reads them.
--
-- Bullets are keyed on the stable STRING id (FBT-ship-g), because the spec makes
-- ids referenceable and mutually-exclusive `claim_group` alternatives share a
-- fact. `entry_key` is the id's prefix (FBT|SIR|ARC|JOB|PEP|FPS) — the
-- role/project a bullet renders under — derived by the loader, not hand-kept.
--
-- The honesty guards live here as JSON arrays, not prose: `must_keep` (phrases a
-- trim may not drop), `no_add` / `no_upgrade` (tokens a trim may not introduce).
-- The load-time assertions in `_load_library` are what make them enforced rather
-- than documented. See docs/resume-redesign.md §4.

CREATE TABLE resume_bullets (
  id                TEXT PRIMARY KEY,     -- stable string id, e.g. FBT-ship-g
  entry_key         TEXT NOT NULL,        -- FBT|SIR|ARC|JOB|PEP|FPS (prefix of id)
  claim_group       TEXT NOT NULL,        -- mutually-exclusive alternatives share one
  framing           TEXT NOT NULL,        -- JSON: "any" or ["general","fullstack",...]
  tier              TEXT NOT NULL CHECK (tier IN ('primary','swap','interview')),
  is_lead_candidate INTEGER NOT NULL DEFAULT 0 CHECK (is_lead_candidate IN (0,1)),
  tags              TEXT,                 -- JSON array
  claim_safety      TEXT,                 -- the human honesty note (kept for the chat/checker)
  must_keep         TEXT,                 -- JSON array: phrases a trim may not drop
  no_add            TEXT,                 -- JSON array: tokens a trim may not introduce
  no_upgrade        TEXT,                 -- JSON array: stricter no_add (hedge -> claim)
  text              TEXT NOT NULL,        -- the resume-ready line, rendered verbatim
  sort_order        INTEGER
);

CREATE INDEX idx_resume_bullets_group ON resume_bullets (claim_group);
CREATE INDEX idx_resume_bullets_entry ON resume_bullets (entry_key);

CREATE TABLE resume_summaries (
  key   TEXT PRIMARY KEY,                 -- general | fullstack | backend
  text  TEXT NOT NULL
);

-- Skills, grouped and ordered. Two tables rather than a JSON blob because the
-- selector reorders groups and items per JD and asserts its output is a
-- permutation/subset of these — cheaper to enforce against rows than to re-parse.
CREATE TABLE resume_skill_groups (
  name       TEXT PRIMARY KEY,
  sort_order INTEGER NOT NULL
);

CREATE TABLE resume_skills (
  group_name TEXT NOT NULL REFERENCES resume_skill_groups(name),
  skill      TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  PRIMARY KEY (group_name, skill)
);

CREATE TABLE resume_variants (
  name          TEXT PRIMARY KEY,         -- general | fullstack | backend
  title_default TEXT NOT NULL,            -- the seniority-guard fallback title
  summary_key   TEXT NOT NULL REFERENCES resume_summaries(key),
  skills_order  TEXT NOT NULL,            -- JSON array of group names
  sort_order    INTEGER
);

-- One row per entry (job or project) a variant renders by default, in order,
-- with its header fields and its default bullet selection. Experience rows fill
-- company/role; project rows fill name/descr/tech. `entry_key` is derived from
-- the entry's bullets' shared prefix and matches resume_bullets.entry_key.
CREATE TABLE resume_variant_entries (
  id              INTEGER PRIMARY KEY,
  variant         TEXT NOT NULL REFERENCES resume_variants(name),
  sort_order      INTEGER NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('experience','project')),
  entry_key       TEXT NOT NULL,
  company         TEXT,                   -- experience only
  role            TEXT,                   -- experience only
  name            TEXT,                   -- project only
  descr           TEXT,                   -- project only
  date_text       TEXT NOT NULL,          -- year-level display range
  tech            TEXT,                   -- project only
  default_bullets TEXT NOT NULL           -- JSON array of bullet ids, in order
);

CREATE INDEX idx_resume_variant_entries ON resume_variant_entries (variant, sort_order);

-- Education for the resume: year-level, free-form date string ("Expected 2027"),
-- which the old `education` table's YYYY-MM start/end columns cannot hold. Its
-- own table rather than a contortion of that one; the old table stays untouched.
CREATE TABLE resume_education (
  id         INTEGER PRIMARY KEY,
  degree     TEXT NOT NULL,
  school     TEXT NOT NULL,
  date_text  TEXT NOT NULL,
  sort_order INTEGER NOT NULL
);

-- Scalar / small-JSON config the library carries that has no natural table:
-- title_default, repos_public (link gating), unconfirmed facts, the optional
-- poster line, gpa. key -> JSON value.
CREATE TABLE resume_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                     -- JSON
);
