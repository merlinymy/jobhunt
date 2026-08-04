-- Index `events` by the column everything joins on.
--
-- `events` had two indexes, both partial and both for uniqueness guards
-- (`digest_sent` once per application, one row per `email_msg_id`). Neither
-- helps a lookup, so every read of an application's history was a full table
-- scan — and `queries.conversion_by` runs two correlated EXISTS subqueries per
-- application to derive "ever reached interview" and "ever reached offer" from
-- history rather than current state.
--
-- Invisible until discovery produced real volume. At 25 hand-entered rows the
-- scan is free; at 4,903 rows from one board poll it is 4,903 x 2 x 4,903 =
-- ~48 million row reads, and the stats page went from 47 ms to 3.5 seconds.
-- It degrades quadratically, so it would have kept getting worse:
--
--   EXPLAIN QUERY PLAN ... CORRELATED SCALAR SUBQUERY 1 / SCAN e
--
-- `to_state` is in the index because both subqueries filter on it, which makes
-- the index covering — SQLite answers the EXISTS from the index alone and never
-- touches the table.
--
-- Not UNIQUE. An application legitimately reaches the same state twice: a
-- rejection after an interview, then a second interview at another stage. The
-- uniqueness guards that do exist are deliberately narrow and stay as they are.

CREATE INDEX idx_events_application ON events (application_id, to_state);

-- `state_rows` and the pipeline view both order by this, and it is the column
-- the inbox poller will scan for recently-updated applications in Phase 5.
CREATE INDEX idx_events_occurred_at ON events (occurred_at);
