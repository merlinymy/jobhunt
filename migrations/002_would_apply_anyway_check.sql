-- Constrain `would_apply_anyway` to 0 / 1 / NULL.
--
-- Why this is a migration and not a code guard: `would_apply_anyway` is the
-- honesty metric, the one CLAUDE.md calls the primary signal. The entry route
-- accepted any integer FastAPI could coerce, so `would_apply_anyway = 7` stored
-- cleanly and the stats page rendered "Would apply anyway — 300%" in green. The
-- ratio is SUM(waa) / COUNT(waa IS NOT NULL), so a single bad row skews it, and
-- such a row matches none of the three dashboard filters (= 1, = 0, IS NULL),
-- which makes it invisible in the UI and therefore unfindable by hand.
--
-- SQLite cannot add a CHECK in place, so this is the documented rebuild: new
-- table, copy, drop, rename. `make migrate` runs each file in one transaction
-- with foreign_keys off and runs foreign_key_check before committing, which is
-- what makes the rename safe for the `events.application_id` reference.
--
-- If the copy below fails with a CHECK violation, the DB already holds an
-- out-of-range value. Nothing here rewrites it — that would be silently editing
-- the honesty record. Find it and decide by hand:
--   SELECT id, would_apply_anyway FROM applications
--    WHERE would_apply_anyway NOT IN (0, 1);

CREATE TABLE applications_rebuild (
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
  would_apply_anyway  INTEGER CHECK (would_apply_anyway IS NULL
                                     OR would_apply_anyway IN (0, 1)),
  applied_at          TEXT,
  first_response_at   TEXT,
  outcome             TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

INSERT INTO applications_rebuild
  SELECT id, job_id, state, score, score_reasoning, resume_pdf, resume_data,
         answers_json, referral_contact_id, would_apply_anyway, applied_at,
         first_response_at, outcome, created_at, updated_at
    FROM applications;

DROP TABLE applications;

ALTER TABLE applications_rebuild RENAME TO applications;

-- Dropped with the old table; 001_init.sql is the reference for what existed.
CREATE INDEX idx_applications_state ON applications (state);
