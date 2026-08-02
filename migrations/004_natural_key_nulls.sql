-- Make `make load-profile` idempotent when a natural key contains a NULL.
--
-- 003 created four natural-key unique indexes so the loader could upsert instead
-- of duplicating the corpus. Two of them span nullable columns:
--
--   idx_education_natural   (school, degree, field)   -- degree, field nullable
--   idx_credentials_natural (kind, title, issuer)     -- issuer nullable
--
-- SQLite treats NULLs as distinct in a unique index, so `ON CONFLICT` never
-- fires for a row whose key contains one and the INSERT succeeds every time. A
-- credential with no issuer, or a degree-in-progress with no degree name, gains
-- a duplicate row on every run. Verified: three runs produced three rows.
--
-- This is the same NULL-distinctness bug 003 fixed for `answers` one screen
-- lower, with the same reasoning; it just wasn't carried up to these two.
-- Latent so far only because docs/profile/ happens to fill every one of those
-- columns today.
--
-- The other two indexes are safe and are left alone: `experiences` keys on
-- (company, title, start_month) and `projects` on (name), all of which the
-- loader requires via _required() and so can never be NULL.
--
-- Fix is an expression index over COALESCE(col, ''), which collapses NULL and
-- '' to one key. Verified that SQLite accepts the same expression as an
-- ON CONFLICT target, which is what lets the loader keep using upserts —
-- load_profile._upsert passes these strings verbatim, so the conflict clause
-- and the index below must stay byte-identical.
--
-- If a CREATE below fails with "UNIQUE constraint failed", this DB already
-- holds duplicates from an earlier run. Nothing here merges them — picking a
-- survivor is a judgement call about your own record. Find them by hand:
--   SELECT school, degree, field, COUNT(*) FROM education
--    GROUP BY school, COALESCE(degree,''), COALESCE(field,'') HAVING COUNT(*) > 1;
--   SELECT kind, title, issuer, COUNT(*) FROM credentials
--    GROUP BY kind, title, COALESCE(issuer,'') HAVING COUNT(*) > 1;
-- Delete the extras, then re-run `make migrate`.

DROP INDEX idx_education_natural;
CREATE UNIQUE INDEX idx_education_natural
  ON education (school, COALESCE(degree, ''), COALESCE(field, ''));

DROP INDEX idx_credentials_natural;
CREATE UNIQUE INDEX idx_credentials_natural
  ON credentials (kind, title, COALESCE(issuer, ''));
