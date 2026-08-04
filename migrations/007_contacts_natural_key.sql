-- Give `contacts` a natural key so `make load-profile` upserts instead of
-- duplicating the whole address book on every run.
--
-- Same class of bug as 004, caught before it shipped rather than after: the
-- loader needs an ON CONFLICT target, and without one a 345-row LinkedIn export
-- becomes 690 rows on the second run and 1,035 on the third.
--
-- Keyed on (name, handle) rather than name alone. Two different people share a
-- name often enough — and the LinkedIn profile URL is what tells them apart.
-- COALESCE for the same reason as 004: SQLite treats NULLs as distinct, so a
-- hand-added contact with no URL would never conflict with itself and would
-- duplicate on every run, which is precisely the case a plain index misses.
--
-- Deliberately not keyed on company. People change jobs, and a contact whose
-- employer changed is the same contact — re-importing after a fresh export
-- should update where they work, not create a second person.

CREATE UNIQUE INDEX idx_contacts_natural
  ON contacts (name, COALESCE(handle, ''));

-- The digest resolves a referral by company on every send, and Phase 5 will
-- want it too. Partial, because a contact with no employer can never match.
CREATE INDEX idx_contacts_company
  ON contacts (company_id) WHERE company_id IS NOT NULL;
