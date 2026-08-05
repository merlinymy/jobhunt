# jobhunt

Personal job-search pipeline. Discovers postings, scores them against my profile, builds a
tailored resume and answer set for each one, and tracks what happens. Single user, localhost
only, runs on a Mac mini.

**I fill out and submit every application by hand.** The system finds the jobs, prepares the
material, and instruments the outcome. It never touches a form and never auto-submits — see
[Why no browser automation](docs/architecture.md#why-no-browser-automation).

## Personal data

`docs/profile/` is **untracked**, and that is deliberate. It holds a legal name, email, phone,
city, pay expectations, a full work history, and — if you export one — a LinkedIn connections
file with other people's names and email addresses in it. That last one is third-party
personal data and is not yours to publish, so none of it belongs in a repository.

- `docs/profile.example/` is the committed template. `cp -r docs/profile.example docs/profile`.
- `JOBHUNT_PROFILE_DIR` moves the real files outside the checkout entirely.
- It used to sync between machines through git. It now syncs with `make profile-push` and
  `make profile-pull` over Tailscale.

This repository's history is clean: `docs/profile/` has never existed in any commit here, and
the phone number, school and personal email that appeared in a few source comments were replaced
throughout. It was filtered out of a private predecessor with `git filter-repo` and verified by
scanning every blob of every commit.

If you fork this and add your own profile, note that a `.gitignore` entry only stops *future*
commits. Anything already committed stays in history and needs `git filter-repo --invert-paths
--path docs/profile/` plus a force-push — and on GitHub, deleting forks and asking support to
garbage-collect, since unreferenced commits stay reachable by SHA for a while.

## Status

| Phase | | State |
| --- | --- | --- |
| 1 | DB, state machine, dashboard | done |
| 2 | Profile store, resume tailoring | done |
| 3 | Answer bank, chat intake | done except the `chat` worker |
| 4 | Discovery, scoring, review queue | done |
| 5 | Gmail inbox poller | not started — `make inbox` will fail until it lands |
| 6 | Interview prep | not started |

## Setup

Requirements: macOS or Linux, Python 3.12 via [uv](https://docs.astral.sh/uv/) (not pyenv, not
conda), Node 20+ for the frontend build, and an Anthropic API key. Nothing else — no Docker, no
database server, no cloud account.

### 1. Install

```bash
git clone <your fork> jobhunt && cd jobhunt
make venv          # uv venv --python 3.12 + the package and its extras
make build-web     # compiles the React app into jobhunt/web/dist
```

### 2. Configure

```bash
cp .env.example .env
```

Three things actually need a value; the rest have working defaults.

| Variable | What to put | Why |
| --- | --- | --- |
| `JOBHUNT_DB` | An absolute path on a **local** disk | Where everything lives. Never iCloud Drive, Dropbox, or any sync folder — WAL plus file-level sync corrupts SQLite, and `db.connect()` refuses such a path outright. A plain `~/jobhunt/jobhunt.db` is fine; an external SSD is what this repo's owner uses, and that path gets extra mount checks. |
| `ANTHROPIC_API_KEY` | A key from [console.anthropic.com](https://console.anthropic.com) | Scoring, tailoring and answer drafting are all model calls. Measured cost is roughly $8–14/month at ~100 applications. Without it, discovery and the dashboard still work; scoring and tailoring do not. |
| `JOBHUNT_PROFILE_DIR` | Leave blank unless your profile lives outside the repo | Defaults to `docs/profile/`. |

If the database goes on an external volume, run `./deploy/install.sh --init-volume` once and
paste the `JOBHUNT_DB_VOLUME_ID` it prints into `.env`. That stamps the disk so an unmounted
volume — or a *different* disk mounted at the same path — is refused rather than silently
written to. See [`docs/deploy-mini.md`](docs/deploy-mini.md).

### 3. Your profile

This is the part that takes real time, and it is the part that decides output quality.
Everything the system writes is drawn from these files; nothing is invented.

```bash
cp -r docs/profile.example docs/profile
```

| File | Required? | What it is |
| --- | --- | --- |
| `facts.yaml` | **yes** | Identity for the resume header, plus the answers you would otherwise improvise differently each time — work authorisation, notice period, walk-away comp. Returned verbatim, never paraphrased by a model. |
| `experience.yaml` | **yes** | The corpus: roles, projects, and one bullet per thing you actually did. The tailor may select, reorder and reword these; it may never introduce an employer, title, date, degree or metric that is not here, and a validator raises if it tries. Write the complete truth and let it cut. |
| `scoring.yaml` | **yes** | What you will and will not take. Title keywords and seniority are the only hard filters; location only *ranks*, and the list ends in a catch-all so it never rejects. |
| `contacts.csv` | optional | People you know, for the referral flag on the review queue. A referral is the single biggest lever on interview rate, so this earns its keep — but the system works fine without it. |
| `Connections.csv` | optional | A LinkedIn connections export, if you have one. Same loader. Manual one-time export only; there is no LinkedIn automation here and there will not be. |
| `stories.md` | optional | Behavioural-interview stories. Only used by Phase 6. |
| `intake-answers.md` | optional | Your working copy of [`docs/intake.md`](docs/intake.md), the questionnaire that produces the files above. Not read by the code. |

Then load them:

```bash
make migrate        # creates the database and applies every migration
make load-profile   # imports docs/profile/* — idempotent, safe to re-run
make doctor         # says whether anything is missing
```

### 4. Run it

```bash
make dev            # http://127.0.0.1:8000
make ingest         # find postings   (needs config/searches.yaml — see below)
make score          # prefilter, then LLM scoring on what survives
```

Discovery reads `config/searches.yaml` for the search terms and the list of company job boards
to poll. The one in this repo is tuned for its owner's search; edit it before your first
`make ingest` or you will get someone else's jobs.

Then open the dashboard, work `/review`, approve what you would genuinely apply to, and build a
packet. **You submit every application yourself.** The system never touches a form.

### Working on the frontend

```bash
make dev            # API on 8000
make dev-web        # Vite on 5173, proxying /api to 8000 — browse this one
make check-web      # eslint + tsc
```

`make build-web` writes into `jobhunt/web/dist`, which is gitignored and rebuilt on the host.
The running server picks up a new build on the next request; no restart needed.

### Running it permanently

[`docs/deploy-mini.md`](docs/deploy-mini.md) is the runbook for an always-on host: three launchd
agents, nightly verified backups, and HTTPS over Tailscale so the review queue works from a
phone. It is macOS-specific.

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

Score orders the review queue and nothing else. I approve every job.

Full detail in [`docs/architecture.md`](docs/architecture.md); the schema in
[`migrations/001_init.sql`](migrations/001_init.sql) is the source of truth.

## The dashboard

A React SPA over a JSON API: Vite, React 19, Tailwind 4, TypeScript, React Router and TanStack
Query, served as static files by the same FastAPI process. One process on the host, no Node at
runtime.

It was server-rendered Jinja + HTMX until 2026-08-05, and the reason it changed was the phone:
the review queue is worth working from anywhere, and five different data shapes had been forced
into one `<table>`. Business logic did not move — `views.py` still decides what a page shows,
`actions.py` what a button does, and the state vocabulary is served from `/api/meta` rather than
hardcoded in TypeScript.

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
- **Only `make migrate` may create a database.** An unmounted `/Volumes` path is still a
  writable path on the boot volume, so every other caller opens `mode=rw` behind an
  `ismount` check and a volume sentinel, and raises rather than starting a decoy.
- **The dashboard binds loopback only.** Cross-machine and phone access is Tailscale Serve
  in front of that port. `serve()` raises on anything that isn't a loopback address.
- **An applied migration may not change.** `migrate.py` stores each file's sha256 and raises if
  it differs; add the next numbered file instead.
- **Illegal state transitions are rejected**, and `log_event()` refuses to forge a
  `state_change`.
- Schema-level: `jobs.apply_url_norm` and `applications.job_id` are UNIQUE, one processed
  email per `email_msg_id`, and natural-key indexes on every table `make load-profile` writes.

## Machines

Three Macs, one host. Only the **Mac mini** runs the app — the repo on its internal disk, the
database on an external encrypted SSD, three launchd agents (discovery, dashboard, backup),
and sleep disabled so the scheduled runs fire. The laptops are dev clients: edit code and
code, and reach the dashboard over Tailscale Serve at
`https://jobhunt-mini.<tailnet>.ts.net`. Profile data is not in git — `make profile-push`
moves it over the tailnet instead. `make doctor` says whether the host is healthy.

## Tests

There is deliberately no test suite. Correctness lives in schema constraints and runtime
assertions, and everything else is verified by hand against real data. Three exceptions are
table-driven with real fixtures: URL normalization, ATS detection, and the tailoring validator
— which must reject an invented employer, a shifted date, or an inflated metric.

```bash
make test        # the two Python suites (standalone scripts, not pytest)
make check-web   # eslint + tsc on the frontend
make doctor      # is this deployment healthy
```
