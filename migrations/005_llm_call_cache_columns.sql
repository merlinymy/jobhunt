-- Make prompt caching auditable, and stop overloading `error`.
--
-- 1. `llm_calls.input_tokens` stores one summed figure: fresh input plus cache
--    reads plus cache writes. That is the right number for "how big was this
--    prompt", but it throws away the only signal that answers "is caching
--    working" — and CLAUDE.md calls caching the first cost lever, ahead of the
--    Batch API and model tier. `cost_usd` is computed from the split and then
--    the split is discarded, so today the hit rate can only be measured by
--    instrumenting outside the app. These two columns keep it.
--
--    A cache read bills at 10% of base input, a write at 125%. So a task whose
--    calls are spread wider than the 5-minute TTL shows all writes and no reads,
--    and is paying a premium for a cache it never gets to use — visible here as
--    cache_write_tokens with cache_read_tokens near zero.
--
-- 2. `stop_reason` gets its own column. llm.py had started writing "max_tokens"
--    into `error`, which is the wrong home: a truncated reply is a successful,
--    billed API call, and folding it into `error` makes "did this call fail"
--    and "was this reply complete" the same question. They are not.
--
-- All three are additive. Existing rows keep NULL, which reads correctly as
-- "recorded before this was tracked" rather than as a zero.

ALTER TABLE llm_calls ADD COLUMN cache_read_tokens  INTEGER;  -- billed at 10%
ALTER TABLE llm_calls ADD COLUMN cache_write_tokens INTEGER;  -- billed at 125%
ALTER TABLE llm_calls ADD COLUMN stop_reason        TEXT;     -- end_turn | max_tokens | refusal | ...

-- The spend panel on the stats page filters by date and groups by task.
CREATE INDEX idx_llm_calls_called_at ON llm_calls (called_at);
