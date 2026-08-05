-- One row per discovery or scoring run: the lock, and the live progress.
--
-- Both workers are far too slow to run inside a request — a sweep is 36 searches
-- at 8-12s of jittered pacing, and a scoring batch is polled for up to 45
-- minutes — so the dashboard button starts them in a background thread and the
-- page watches this table. That makes "is something already running?" a question
-- the database has to answer, not the process.
--
-- It has to be the database because the callers are separate processes. The
-- launchd agent runs `ingest` then `score` at 06:30 and 18:30 from its own
-- process; a click at 06:31 comes from the dashboard's. A flag in memory sees
-- one of those and not the other, and the two failures that produces are the two
-- worth preventing: a second concurrent Indeed scrape, which is how a soft
-- throttle becomes a ban, and a second scoring batch over the same rows, which
-- is billed twice for one answer.
--
-- `idx_runs_one_active` is that guard, and it is a partial unique index rather
-- than a SELECT-then-INSERT in Python for the reason the rest of this schema
-- prefers constraints: a double-click is two requests already in flight, and a
-- check-then-write loses that race. Here the second INSERT raises IntegrityError
-- and the caller answers 409. Same shape as `idx_events_email_once`.
--
-- Per task, not one global lock. Scoring holds its row for the whole 45-minute
-- batch wait, and blocking a discovery sweep behind that would mean the morning
-- postings arrive after lunch. The two touch different rows and different money.
--
-- `chain` is the pipeline the run belongs to — 'ingest,score' for the button
-- that does both. Stored rather than derived so a page opened halfway through
-- can still say "step 2 of 2" without the client having guessed what was asked
-- for. `progress` is a JSON snapshot, overwritten in place perhaps once a
-- second: phase, a sentence, done/total, and the running tallies. Deliberately
-- not history — nobody wants a row per search, and the finished snapshot is the
-- summary the dashboard shows afterwards.
--
-- `heartbeat_at` is what makes a crash recoverable. A killed process leaves its
-- row `running` forever and the lock is then held by nobody, so a claim first
-- reclaims rows whose owner is provably gone: the pid is dead on this host, or
-- nothing has reported for ten minutes. See `runs.reclaim_stale`.

CREATE TABLE runs (
  id           INTEGER PRIMARY KEY,
  task         TEXT NOT NULL CHECK (task IN ('ingest', 'score')),
  chain        TEXT NOT NULL,              -- 'ingest' | 'score' | 'ingest,score'
  state        TEXT NOT NULL CHECK (state IN ('running', 'done', 'failed', 'interrupted')),
  trigger      TEXT NOT NULL CHECK (trigger IN ('dashboard', 'cli', 'launchd')),
  host         TEXT NOT NULL,
  pid          INTEGER NOT NULL,
  started_at   TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  finished_at  TEXT,
  progress     TEXT,                       -- JSON: phase, message, done, total, counts
  error        TEXT,

  -- A finished run with no finish time is the state that makes every "how long
  -- ago" query lie, and a running one with a finish time is a half-written
  -- release. Neither is representable.
  CHECK ((state = 'running') = (finished_at IS NULL))
);

-- The lock itself.
CREATE UNIQUE INDEX idx_runs_one_active ON runs (task) WHERE state = 'running';

-- "the last run of each task", which is the dashboard's idle line and every
-- staleness question doctor asks.
CREATE INDEX idx_runs_recent ON runs (task, id DESC);
