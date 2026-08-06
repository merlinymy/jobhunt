-- Which job or project a story happened at.
--
-- `stories.experience_id` has existed since 001 and has never been populated:
-- `_parse_stories` read `**Role:**` out of the file and then dropped it on the
-- floor, so every story loaded unattached. Nothing caught it because nothing had
-- ever filled stories.md in.
--
-- `project_id` is the other half, and it is the half that matters here. This
-- corpus is one paid role and five projects, so a story bank linked only to
-- employment could attach almost nothing — of the nine drafted stories, eight
-- happened on projects.
--
-- Nullable on purpose. A story that spans two projects, or none, is still a
-- story; the link is for filtering, not for provenance, and refusing to load an
-- unattachable one would lose the story to keep the index tidy.
ALTER TABLE stories ADD COLUMN project_id INTEGER REFERENCES projects(id);

CREATE INDEX stories_experience ON stories (experience_id);
CREATE INDEX stories_project    ON stories (project_id);
