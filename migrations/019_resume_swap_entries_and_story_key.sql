-- Renderer support (Phase 1 Checkpoint 3): swap-in project entries, and the
-- story -> library entry_key link. Both additive; the old corpus and the
-- experiences/projects tables are untouched.
--
-- resume_swap_entries holds the project entries a variant does not carry by
-- default (peptide, game) but the selector swaps in for research or game
-- postings. A swap-in project descriptor is as much a claim surface as a bullet,
-- so it is authored under the same honesty constraints and rendered verbatim —
-- never re-derived or interpolated by the renderer.
--
-- stories.entry_key repoints story role-resolution off the archived
-- experiences/projects tables and onto the library, so a fresh clone resolves a
-- story to its role instead of loading it unattached. The old experience_id /
-- project_id columns stay (now unfed, not dropped).

CREATE TABLE resume_swap_entries (
  entry_key TEXT PRIMARY KEY,     -- PEP | FPS (matches resume_bullets.entry_key)
  kind      TEXT NOT NULL CHECK (kind IN ('experience','project')),
  name      TEXT NOT NULL,
  descr     TEXT,
  date_text TEXT NOT NULL,
  tech      TEXT
);

ALTER TABLE stories ADD COLUMN entry_key TEXT;   -- the library entry a story happened at
