-- Conversation about one application's resume.
--
-- Scoped to an application rather than global: every exchange is about this
-- posting and this draft, and a thread that spans jobs would carry the last
-- job's framing into the next one — the same reason `tailor_check` is not shown
-- the job description.
--
-- `proposal` holds the revised selection the assistant offered, as
-- {"summary": ..., "bullets": [{"id": N, "text": "..."}]} — the same shape
-- `tailor.parse_response` returns, so an applied proposal goes through exactly
-- the render-and-store path a build does. NULL when the reply was only an answer
-- and changed nothing, which is a legitimate turn: "why did you drop the Redis
-- bullet" deserves a sentence, not a new resume.
--
-- `applied_at` is what makes a proposal a record rather than a suggestion. It is
-- set once; re-applying an older proposal writes a new row rather than mutating
-- this one, so the thread stays an append-only account of what was actually done.
CREATE TABLE packet_chat (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  role           TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
  content        TEXT    NOT NULL,
  proposal       TEXT,
  applied_at     TEXT,
  created_at     TEXT    NOT NULL
);

-- The only read this table gets: one application's thread, oldest first.
CREATE INDEX packet_chat_application ON packet_chat (application_id, id);
