-- Editable prompt revisions, keyed by the same sha `llm_calls` already records.
--
-- Prompt wording is the thing most worth iterating on and the slowest to
-- iterate on: edit a file, save, click, read a resume, decide, edit again. Doing
-- that from the dashboard instead of a second window is the whole point of this
-- table.
--
-- An overlay, not a replacement. `config/prompts/<task>.md` stays the default
-- and stays git-tracked, and a task with no active row here reads the file
-- exactly as before. That keeps three things a pure-database prompt would cost:
-- the repo still works for someone who clones it, a prompt edit is still
-- reviewable as a diff once you copy a revision back into the file, and
-- resetting the database does not silently lose the wording that produced every
-- resume you have sent. `Revert to file` is therefore always available.
--
-- Keyed by (task, sha) rather than by task alone, because the interesting
-- question is not "what is the prompt" but "which prompt wrote this resume".
-- `llm_calls.system_sha` is twelve hex chars of sha256 over the prompt text as
-- sent; storing revisions under the same key makes that column a join instead
-- of an archaeology exercise, including for revisions since replaced. Editing
-- back to a previous wording re-activates that row rather than making a new
-- one, so the history stays a set of distinct wordings.
--
-- `idx_prompts_active` is the same partial-unique idiom as `runs`: at most one
-- active revision per task, enforced rather than assumed.

CREATE TABLE prompts (
  task       TEXT NOT NULL,
  sha        TEXT NOT NULL,             -- matches llm_calls.system_sha
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
  note       TEXT,                      -- why this wording, in your own words
  PRIMARY KEY (task, sha)
);

CREATE UNIQUE INDEX idx_prompts_active ON prompts (task) WHERE active = 1;
