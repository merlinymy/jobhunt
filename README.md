# jobhunt

A job-search pipeline for one person. It discovers postings, scores them against your
profile, builds a tailored resume and answer set for each one, and tracks what happens. Single
user, binds to localhost, runs on any Mac or Linux box you leave on.

**You fill out and submit every application by hand.** The system finds the jobs, prepares the
material, and instruments the outcome. It never touches a form and never auto-submits — see
[Why no browser automation](docs/architecture.md#why-no-browser-automation).

The objective it is tuned for is *interviews per application submitted*, not applications sent.
Filters exist to drop roles you would genuinely refuse; the review queue asks
*would I have applied to this anyway?* on every one, and a falling ratio there is the signal
that the thing has quietly become a volume machine.

## Personal data

`docs/profile/` is **untracked**, and that is deliberate. It holds a legal name, email, phone,
city, pay expectations, a full work history, and — if you export one — a LinkedIn connections
file with other people's names and email addresses in it. That last one is third-party
personal data and is not yours to publish, so none of it belongs in a repository.

- `docs/profile.example/` is the committed template. `cp -r docs/profile.example docs/profile`.
- `JOBHUNT_PROFILE_DIR` moves the real files outside the checkout entirely.
- Editing on one machine and running on another? `make profile-push HOST=…` and
  `make profile-pull HOST=…` rsync it over ssh, since git is no longer carrying it.

A `.gitignore` entry only stops *future* commits. If you ever commit a profile by accident,
it stays in history until `git filter-repo --invert-paths --path docs/profile/` and a
force-push — and on GitHub, until you also delete forks and ask support to garbage-collect,
because unreferenced commits stay reachable by SHA for a while.

## What works

Discovery, scoring, the review queue, resume tailoring, the answer bank and the dashboard are
all built and in use. Two things are declared in the `Makefile` but not implemented yet, and
will fail with `ModuleNotFoundError` until they are: `make inbox` (Gmail polling to close
applications automatically) and `make chat` (interactive gap-filling for questions the answer
bank has no entry for). Everything else in `make help` runs.

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

**Only one variable actually needs a value.** Everything else has a working default, and the
shipped `.env.example` leaves it that way — the commented-out lines are options, not blanks to
fill in.

| Variable | What to put | Why |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | A key from [console.anthropic.com](https://console.anthropic.com) | Scoring, tailoring and answer drafting are all model calls. Measured cost is roughly $8–14/month at ~100 applications. Without it, discovery and the dashboard still work; scoring and tailoring do not. |

Two you may want later, both commented out by default:

| Variable | Default | When to change it |
| --- | --- | --- |
| `JOBHUNT_DB` | `jobhunt.db` in the repo root, gitignored | Only if you want the database elsewhere. It must be on a **local** disk — never iCloud Drive, Dropbox, or any sync folder, since WAL plus file-level sync corrupts SQLite. `db.connect()` refuses such a path outright rather than letting you find out later. |
| `JOBHUNT_PROFILE_DIR` | `docs/profile/` | Only if you keep your profile outside the checkout. |

**If — and only if — you point `JOBHUNT_DB` at an external disk**, run
`./deploy/install.sh --init-volume` once with it mounted and paste the `JOBHUNT_DB_VOLUME_ID`
it prints into `.env`. A path under `/Volumes` gets three extra checks, because an unmounted
`/Volumes` path is still a writable path on the boot volume: without them, opening the database
with the disk absent creates a decoy that goes silently missing the moment the real disk mounts
beside it. Skipping this step is not dangerous — it just means `make migrate` refuses to run
until the disk is mounted and stamped. See [`docs/deploy-mini.md`](docs/deploy-mini.md).

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

`config/searches.yaml` holds the search terms and the company job boards to poll. **Edit it
before your first `make ingest`** — the version in this repo is one person's search, so as
shipped it will find you someone else's jobs.

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

Nothing above needs a server — `make dev` on a laptop is a complete install. If you want it
running unattended, [`docs/deploy-mini.md`](docs/deploy-mini.md) is a worked example on macOS:
three launchd agents for discovery, the dashboard and nightly verified backups, the database on
an external disk, and HTTPS over Tailscale so the review queue works from a phone. Adapt or
ignore it; the app itself does not care.

## How it works

One state machine on one table. Each worker picks up rows in one state and moves them to the
next; there is no broker and no scheduler beyond launchd.

Transitions are enumerated rather than drawn — `docs/architecture.md` notes that the
column-aligned ASCII version was ambiguous about which state each branch hung off, and had two
of them wrong. The happy path is `discovered → scored → job_approved → packet_ready → applied
→ interview → offer`, and every state has exactly these exits:

| From | To |
| --- | --- |
| — | `discovered` (ingest), `applied` (manual entry of one already submitted by hand) |
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

Score orders the review queue and nothing else. You approve every job.

Full detail in [`docs/architecture.md`](docs/architecture.md); the schema in
[`migrations/001_init.sql`](migrations/001_init.sql) is the source of truth.

## The dashboard

A React SPA over a JSON API: Vite, React 19, Tailwind 4, TypeScript, React Router and TanStack
Query, served as static files by the same FastAPI process. One process on the host, no Node at
runtime.

Business logic stays in Python: `views.py` decides what a page shows, `actions.py` what a
button does, and the state vocabulary comes from `/api/meta` rather than being hardcoded in
TypeScript. It works on a phone — the review queue is the flow worth having in your pocket.

- **Review** — the curated queue and the only place a job is approved. One card per posting
  with its score, the job description behind a button, and a referral flag if you know someone
  there. Duplicate listings of the same role are collapsed to one card, so deciding it decides
  all of them. Skip is terminal, so it sits behind a three-second undo.
- **Packet** — everything needed to fill one form: the tailored resume PDF, a diff against
  your corpus showing exactly what was reworded, and the answer set with a copy button on each.
- **Pipeline** — funnel counts and the applications table, with six composing filters and eight
  sortable columns. Filters and sort live in the query string, so any view is a bookmarkable
  URL that survives a reload.
- **Fill** — every field an ATS asks for, one per box, with dates rendered in each of the four
  formats they disagree about. Not autofill; just the retyping removed.
- **Log application** — manual entry for anything submitted outside the pipeline. Checks the
  apply URL live against the dedup key and refuses a posting already tracked.
- **Stats** — conversion by ATS, source and referral status, each independently sortable, plus
  the `would_apply_anyway` ratio.

Auto / light / dark switch in the header, stored in `localStorage` and applied by a blocking
script before first paint, so a pinned dark theme never flashes white.

## Layout

```
jobhunt/
  config.py      paths, .env reader, the only clock
  db.py          connections, pragmas, transactions, the volume guard
  doctor.py      one answer to "is this deployment healthy"
  backup.py      VACUUM INTO snapshots, verified and pruned
  migrate.py     applies migrations, refuses edited ones
  normalize.py   URL normalization, ATS detection, dedup keys
  states.py      the state machine — every state write goes through here
  queries.py     named parameterized SQL, plus filtering and stats aggregation
  ingest.py  prefilter.py  score.py  tailor.py  resume.py  answers.py  llm.py
  web/
    app.py       the shell: mounts, error handlers, SPA fallback
    api.py       the JSON API — routing and serialization only
    views.py     what a page shows (no framework in it)
    actions.py   what a button does (no framework in it)
web/             the React app; builds into jobhunt/web/dist
config/
  models.yaml    which model runs which task, and why
  prompts/       one system prompt per task, edited without a restart
  searches.yaml  search terms and the boards to poll — edit this
docs/
  architecture.md    state machine, workers, algorithms, packet design
  intake.md          the questionnaire that fills docs/profile/
  profile.example/   template for docs/profile/, which is untracked
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

## Commands

`make` on its own lists every target.

| | |
| --- | --- |
| `make dev` | dashboard on localhost:8000 |
| `make dev-web` | Vite on 5173 for frontend work, proxying `/api` to 8000 |
| `make migrate` | apply `migrations/*.sql` in order; idempotent |
| `make load-profile` | import `docs/profile/*`; idempotent |
| `make ingest` | one discovery run — JobSpy plus direct ATS board polls |
| `make score` | deterministic prefilter, then LLM scoring on the survivors |
| `make tailor` | build packets for everything in `job_approved` |
| `make backup` | snapshot the database, verify it, prune old ones |
| `make doctor` | disk, schema, backups, dependencies, bind address |
| `make test` | the two table-driven suites |
| `make check-web` | eslint and tsc |

There is deliberately no broad test suite — correctness lives in schema constraints and
runtime assertions that raise. The two exceptions are table-driven against real fixtures: URL
normalization, and the tailoring validator, which has to reject an invented employer, a shifted
date, and an inflated metric.
