# jobhunt

Personal job-search pipeline. Discovers postings, scores them against my profile, builds a
tailored resume and answer set for each one, and tracks what happens. Single user, localhost
only, runs on a Mac mini.

**I fill out and submit every application by hand.** The system finds the jobs, prepares the
material, and instruments the outcome. It never touches a form and never auto-submits — see
[Why no browser automation](docs/architecture.md#why-no-browser-automation).

> **Keep this repository private.** `docs/profile/` holds my address, phone, salary
> expectations, references, and EEO answers. It is committed on purpose — it's how profile data
> syncs between machines — which is exactly why the repo cannot be public.

## Status

Phase 1 is done. Phase 2 is next and is gated on my own data entry, not on code.

| Phase | | State |
| --- | --- | --- |
| 1 | DB, state machine, dashboard | **done** — backfill of hand-submitted applications still pending |
| 2 | Profile store, resume tailoring | next — needs `docs/profile/experience.yaml` filled |
| 3 | Answer bank, chat intake | not started |
| 4 | Discovery, scoring, Telegram digest | not started |
| 5 | Gmail inbox poller | not started |

Only two commands are implemented so far. The rest are declared in the `Makefile` as the
interface to build against, and will fail with `ModuleNotFoundError` until their phase lands.

| Command | | |
| --- | --- | --- |
| `make migrate` | apply `migrations/*.sql` in order | **live** |
| `make dev` | dashboard on localhost:8000 | **live** |
| `make load-profile` | import `docs/profile/*` into SQLite | Phase 2 |
| `make tailor` | build packets for `job_approved` | Phase 2 |
| `make chat` | resolve `unknown_questions`, append answers | Phase 3 |
| `make ingest` | one-shot discovery run | Phase 4 |
| `make score` | prefilter + LLM scoring | Phase 4 |
| `make digest` | send today's Telegram digest | Phase 4 |
| `make inbox` | poll Gmail, classify, update states | Phase 5 |

## Setup

Python 3.12 via [uv](https://docs.astral.sh/uv/). Not pyenv, not conda.

```bash
make venv                     # uv venv --python 3.12, then installs the package
cp .env.example .env          # then set JOBHUNT_DB to a path on LOCAL disk
make migrate                  # creates the DB, WAL mode, applies 001_init.sql
make dev                      # http://127.0.0.1:8000
```

`make` on its own lists every target. `make migrate` is idempotent — re-running it on an
up-to-date DB prints `up to date` and changes nothing.

For local dev on a laptop, point `JOBHUNT_DB` at a throwaway file. The real database lives on
the mini and only the mini's process opens it.

## How it works

One state machine on one table. Each worker picks up rows in one state and moves them to the
next; there is no broker and no scheduler beyond launchd.

Transitions are enumerated rather than drawn — `docs/architecture.md` notes that the
column-aligned ASCII version was ambiguous about which state each branch hung off, and had two
of them wrong. The happy path is `discovered → scored → job_approved → packet_ready → applied
→ interview → offer`, and every state has exactly these exits:

| From | To |
| --- | --- |
| — | `discovered` (ingest), `applied` (manual entry of one I submitted by hand) |
| `discovered` | `scored`, or `filtered` by the deterministic prefilter |
| `scored` | `job_approved`, or `skipped` |
| `job_approved` | `packet_ready`, or `expired` |
| `packet_ready` | `applied`, or `expired` |
| `applied` | `interview`, or `rejected` |
| `interview` | `offer`, or `rejected` |

`filtered`, `skipped`, `expired`, `rejected`, and `offer` are terminal.

Every state change writes an `events` row in the same transaction. `jobhunt/states.py` is the
only place `applications.state` is written, and it rejects any transition absent from its
table — including `applied → offer`, since an offer always passes through `interview`.

Score orders the digest and nothing else. I approve every job.

Full detail in [`docs/architecture.md`](docs/architecture.md); the schema in
[`migrations/001_init.sql`](migrations/001_init.sql) is the source of truth.

## The dashboard

Three pages, server-rendered with FastAPI + Jinja + HTMX. No build step, no bundler, no JS
framework — htmx is vendored into `static/`, so it works offline.

- **Pipeline** — funnel counts, and the applications table with six composing filters (search,
  state, ATS, source, referral, would-apply-anyway) and eight sortable columns. Filters and
  sort live in the query string, so any view is a bookmarkable URL that survives a reload.
- **Log application** — manual entry for anything submitted by hand. Checks the apply URL live
  against the dedup key and refuses a posting already tracked. Always asks
  *would I have applied to this anyway?*
- **Stats** — conversion by ATS, by source, and by referral status, each independently
  sortable, plus the `would_apply_anyway` ratio. A falling ratio means the system is
  manufacturing volume, which is the thing this design exists to prevent.

Auto / light / dark theme switch in the header, stored in a cookie and applied server-side.

## Layout

```
jobhunt/
  config.py      paths, .env reader, the only clock
  db.py          connections, pragmas, transactions
  migrate.py     applies migrations, refuses edited ones
  normalize.py   URL normalization, ATS detection, dedup keys
  states.py      the state machine — every state write goes through here
  queries.py     named parameterized SQL, plus filtering and stats aggregation
  web/           FastAPI app, Jinja templates, CSS, vendored htmx
config/models.yaml   which model runs which task, and why
docs/
  architecture.md    state machine, workers, algorithms, packet design
  build-plan.md      phases and gates — check boxes off as work completes
  intake.md          the questionnaire that fills docs/profile/
  profile/           my actual profile data. Source of truth, hand-edited.
migrations/          numbered .sql, applied in order, never edited after running
```

## Rules the code enforces

These raise at runtime rather than living only in a document:

- **The DB may not sit in a sync folder.** WAL plus file-level sync corrupts SQLite, so
  `db.connect()` refuses any path under iCloud Drive, Dropbox, Google Drive, or OneDrive.
- **The dashboard binds loopback only.** Cross-machine and phone access is Tailscale to the
  mini's port. `run_dev()` raises on anything that isn't a loopback address.
- **An applied migration may not change.** `migrate.py` stores each file's sha256 and raises if
  it differs; add the next numbered file instead.
- **Illegal state transitions are rejected**, and `log_event()` refuses to forge a
  `state_change`.
- Schema-level: `jobs.apply_url_norm` and `applications.job_id` are UNIQUE, one `digest_sent`
  event per application ever, one processed email per `email_msg_id`.

## Machines

Three Macs, one host. Only the **Mac mini** runs the app — it holds the database, the launchd
jobs, and the dashboard, with sleep disabled so the morning digest fires. The laptops are dev
clients: edit code and profile data, push through git, and reach the dashboard over Tailscale.
Profile data syncs via git, never through the database.

## Tests

There is deliberately no test suite. Correctness lives in schema constraints and runtime
assertions, and everything else is verified by hand against real data. Three exceptions are
table-driven with real fixtures: URL normalization, ATS detection, and the tailoring validator
— which must reject an invented employer, a shifted date, or an inflated metric.
