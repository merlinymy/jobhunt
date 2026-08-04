-- Natural key for `stories`, so `make load-profile` upserts them.
--
-- Third time for this shape (004 for education and credentials, 007 for
-- contacts), and the last table the loader writes without one. `stories.md` is
-- edited and reloaded repeatedly by design — it is prose, and prose gets
-- rewritten — so without this every reload appends another copy of every story.
--
-- Keyed on title alone. Unlike a contact, a story is identified by what it is
-- about; two stories with the same title are the same story rewritten, which is
-- exactly the case that should update rather than duplicate. Titles are
-- hand-written in one file by one person, so a collision is a mistake worth
-- surfacing rather than a real pair to keep apart.
--
-- Not COALESCE'd: `title` is NOT NULL in 001, so the NULL-distinctness problem
-- that 004 and 007 had cannot arise here.

CREATE UNIQUE INDEX idx_stories_title ON stories (title);

-- The tailor and the answer bank both reach for stories by tag. Cheap, and
-- `tags` is a JSON array so this only helps the scans that filter on presence —
-- it is here because `stories` will be read on every packet build.
CREATE INDEX idx_stories_experience ON stories (experience_id)
  WHERE experience_id IS NOT NULL;
