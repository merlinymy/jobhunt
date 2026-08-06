-- Let `runs` carry packet builds as well as discovery and scoring.
--
-- Approving a job in /review used to leave five clicks between the decision and
-- the thing the decision was for: pipeline, filter to job_approved, open the
-- row, open the packet, press build. The packet now starts itself the moment a
-- job is approved, which means it needs somewhere to run — and everything a
-- background build needs already exists here. One at a time, progress on the
-- page, a reclaimable lock, and `make doctor` can see it.
--
-- Its own task rather than a third step of the discovery chain: a packet build
-- is triggered by a decision, not by a sweep, and pairing them would mean
-- approving a job during a scoring batch had to wait forty-five minutes.
--
-- SQLite cannot widen a CHECK in place, so this is the documented rebuild —
-- create, copy, drop, rename — the same dance 002 did to `applications`.
-- `migrate.py` runs the whole file in one transaction with foreign_keys off and
-- runs foreign_key_check before it commits. `runs` is referenced by nothing and
-- references nothing, so there is no dangling anything to repoint.

CREATE TABLE runs_new (
  id           INTEGER PRIMARY KEY,
  task         TEXT NOT NULL CHECK (task IN ('ingest', 'score', 'packet')),
  chain        TEXT NOT NULL,
  state        TEXT NOT NULL CHECK (state IN ('running', 'done', 'failed', 'interrupted')),
  trigger      TEXT NOT NULL CHECK (trigger IN ('dashboard', 'cli', 'launchd')),
  host         TEXT NOT NULL,
  pid          INTEGER NOT NULL,
  started_at   TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  finished_at  TEXT,
  progress     TEXT,
  error        TEXT,
  CHECK ((state = 'running') = (finished_at IS NULL))
);

INSERT INTO runs_new (id, task, chain, state, trigger, host, pid, started_at,
                      heartbeat_at, finished_at, progress, error)
  SELECT id, task, chain, state, trigger, host, pid, started_at,
         heartbeat_at, finished_at, progress, error
    FROM runs;

DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;

-- Both indexes come with the old table. Recreated verbatim from 010: the lock
-- itself, and the per-task recency lookup behind the dashboard's idle line.
CREATE UNIQUE INDEX idx_runs_one_active ON runs (task) WHERE state = 'running';
CREATE INDEX idx_runs_recent ON runs (task, id DESC);
