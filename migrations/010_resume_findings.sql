-- What the checks objected to in the resume currently stored on this row.
--
-- A JSON array of `tailor.Finding`. It has to be persisted rather than
-- recomputed on view, because one of the three checks is a model call: showing
-- the findings on every page load would bill for a checker run every time you
-- opened a packet.
--
-- NULL means "never built". An empty array means "built and nothing objected",
-- which is a different and much more useful statement.
ALTER TABLE applications ADD COLUMN resume_findings TEXT;
