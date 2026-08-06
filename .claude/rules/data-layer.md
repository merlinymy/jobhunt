---
paths:
  - "migrations/*.sql"
  - "**/db.py"
  - "**/queries*.py"
  - "**/models*.py"
---

# Data layer rules

Correctness lives in constraints, not tests. When there's a choice between a runtime check and
a schema constraint, choose the constraint.

## Migrations

- Numbered `.sql` files applied in order. Never edit one that has already run — add the next.
- `PRAGMA foreign_keys = ON` on every connection, not just at creation. SQLite defaults it off
  per-connection and silently ignores FK violations otherwise.
- WAL mode. Set once, verify on startup.

## Non-negotiable constraints

These exist to make bad states unrepresentable rather than merely untested:

- `jobs.apply_url_norm` UNIQUE — the hard dedup guard
- `applications.job_id` UNIQUE — one application per job, ever
- `applications.state` CHECK against the enumerated states
- `answers.tier` CHECK in (`fact`, `narrative`)
- `answers (question_key, company_id)` UNIQUE — no two answers to one question per company
- `bullets` CHECK that exactly one of `experience_id` / `project_id` is set
- Partial unique index on `events.email_msg_id` — an email is processed once
  (the `digest_sent` index from 001 is now unused: the Telegram digest was replaced by
  the `/review` pull queue, which needs no send-once guard. Left in place — migrations
  are never edited after they run, and an unused index costs nothing.)

## Writing rules

- Every `applications.state` change writes an `events` row in the same transaction. If it can't
  be one transaction, it's wrong.
- Never `UPDATE applications SET state = ...` outside the single state-transition helper.
- `resume_pdf` and `answers_json` are frozen the moment an application is submitted, and never
  updated after that. They are the record of what was actually sent, nothing can regenerate
  it, and reconstructing one from current templates is forbidden outright.

  Before submission they are a draft. A `packet_ready` row can be rebuilt — that is why
  `states.py` keeps `packet_ready` out of `SUBMITTED_STATES`, and without it no prompt change
  could ever be tried against a job already approved. The rebuild overwrites the bytes and
  writes a `note` event saying so, so a resume that changed under you is visible in the
  history. `tailor.BUILDABLE_STATES` is the enforcement; `SUBMITTED_STATES` is the wall.
- Timestamps: ISO 8601 UTC, TEXT, generated in one place. Never `datetime('now')` inline in
  scattered queries.

## Query module

Thin. Parameterized SQL in named functions, returning `sqlite3.Row` or plain dicts. No ORM, no
query builder, no lazy relationships. If a query is complex enough to want an ORM, write the
SQL and name the function well.

## Profile loading

`docs/profile/*` is the source of truth for my profile data; the DB is derived. The loader:

- Upserts, never duplicates. Re-running on unchanged files is a no-op.
- Is safe to run mid-fill — blank and missing keys are skipped, not written as empty strings.
- Never deletes DB rows that the YAML no longer mentions without an explicit `--prune` flag.
- Does not touch `answers` rows with `source = 'generated'`. Those accumulate in the DB only
  and have no file representation.
- Fails loudly on malformed YAML rather than partially importing.

Do not build a CRUD editor for profile data. Editing the files is the interface.
