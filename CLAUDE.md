# Job Application System

Personal job-search pipeline. Discovers jobs, scores them against my profile, generates a
tailored resume and answer set per job, tracks outcomes. **I fill out and submit every
application by hand.** Single user, localhost only, runs on a Mac mini M4.

**Objective: maximize interviews per application I hand-submit.** The binding constraint is my
~10 minutes per form, not the supply of postings — so the job is to make each submission count,
not to raise the count. Filters exist to drop roles I would genuinely refuse, never to discard
shots on unreliable or missing data. `would_apply_anyway` is the check that this hasn't quietly
become volume; read it as the primary metric, not a footnote.

**What I select on, in order: title, level, then location.** Title and level are the only hard
filters. Location *ranks* the review queue and never rejects — the preference list ends in a
catch-all. Comp neither filters nor ranks; it exists to answer with. When adding a rule, decide
first whether it rejects or merely orders, and default to ordering.

<!-- Detail deliberately kept out of this file to stay under the 200-line context budget.
     Read the docs below on demand; do not @import them. -->

Read on demand, not every session:

- `docs/architecture.md` — state machine, components, algorithms, packet design
- `docs/build-plan.md` — phases, gates, current status (check boxes off as work completes)
- `docs/deploy-mini.md` — bringing the host up, in order, with what each step proves
- `migrations/` — schema, source of truth
- `config/models.yaml` — which model runs which task, and why
- `docs/intake.md` — profile questionnaire; answers go in `docs/profile/`
- `docs/profile/` — my actual profile data. **Source of truth**, hand-edited, versioned.

## Non-goals — do not build these, do not propose them

- **No browser automation.** No Playwright, Puppeteer, Selenium, browser-use, Skyvern, or
  autofill extension. Form filling is manual by design — decided against error compounding
  across multi-step ATS wizards. If a task appears to need a browser, stop and ask.
- **No LinkedIn automation.** LinkedIn data enters only through a manual one-time CSV export.
- **No mass applying.** ~100 applications/month, each individually reviewed. Never auto-submit.
- **No account creation automation.** I create ATS accounts myself.
- **No cloning or vendoring third-party job bots** — not AIHawk, Skyvern, browser-use, or
  Resume-Matcher. Four libraries plus our own code.
- **No auth, no multi-user, no Docker, no cloud deploy.** The dashboard *is* a React SPA
  as of 2026-08-05 — that line used to say "no SPA", and the reason it changed is that the
  review queue needed to work on a phone and five data shapes had been forced into one
  `<table>`. Everything else on this list still holds.

## No test suite

Do not write tests unless I ask. Three exceptions, table-driven with real fixtures:

1. URL normalization — ~15 real apply URLs → expected output
2. ATS detection regex — same
3. Tailoring validator — adversarial fixtures (invented employer, shifted date, inflated
   metric) must be rejected

Everything else is verified manually against real data. Prefer DB constraints and runtime
assertions that raise over tests that assert whatever the code currently does.

## Invariants

- Every state change writes an `events` row. Never mutate `applications.state` silently.
- `applications.resume_pdf` and `answers_json` hold the **exact** bytes and JSON submitted.
  Never reconstruct a past submission from current templates.
- Tailoring may select, reorder, and reword rows from `bullets`. It may never introduce an
  employer, title, date, degree, or metric absent from those rows. Two checks enforce it and
  the split is deliberate: **numbers stay in code** — every figure and identifier in the
  output must appear in its own source row, which is exact comparison and where a model is
  unreliable — and **everything that is reading goes to a model**, on a different tier from
  the one that wrote it. Regex guessing at whether `Stopped` was a verb or a company produced
  every false rejection this system ever made. Both raise; neither warns.
- `answers.tier = 'fact'` is returned verbatim. Never LLM-generated, never paraphrased.
- Workers are stateless and idempotent. Rerunning must not duplicate rows. Guard on
  `jobs.apply_url_norm` and on `events`.
- Score never advances state. It only orders the review queue. I approve every job.
- `docs/profile/` is the source of truth for my profile; the DB is derived. Build a loader,
  never a CRUD editor for this data. Generated per-company narrative answers are the one
  exception — they accumulate in the DB only.

## Model routing

API for everything. No local models — measured cost at ~100 applications/month is ~$14/mo
all-Opus, ~$8/mo with Haiku on bulk tasks. Not worth the RAM, latency, or the dependency.

Model per task lives in `config/models.yaml`. **Never hardcode a model ID at a call site.**
All calls go through one `llm.py` with a `complete(task, prompt)` signature, so swapping
tiers — or adding an Ollama backend later — is a config change, not a refactor.

Same rule for the wording: a task's system prompt is `config/prompts/<task>.md`, never a
constant in the worker. Re-read on every call, so editing one needs no restart. `llm.py`
logs its sha to `llm_calls.system_sha` — that column is how output quality gets attributed
to a revision. `config/prompts/README.md` records which instructions the validator and the
response parsers depend on; those are not free text.

Cost levers in order of real impact: prompt caching first (the profile corpus is
byte-identical across every call), then the Batch API for scoring, then model tier last.

Log prompt, response, token counts, cost, and latency to `llm_calls` on every call.

## Commands

Intended interface — build to this contract.

```bash
make migrate       # apply migrations/*.sql in order
make load-profile  # import docs/profile/* into SQLite; idempotent, re-runnable
make dev           # API + built bundle on localhost:8000
make dev-web       # Vite on 5173 proxying /api to 8000 — the frontend loop
make build-web     # compile the React app into jobhunt/web/dist
make check-web     # eslint + tsc
make ingest        # one-shot discovery run
make score         # prefilter + LLM scoring on `discovered`
make tailor        # build packets for `job_approved`
make inbox         # poll Gmail, classify, update states
make chat          # gap-filling: resolve unknown_questions, append answers
make doctor        # is the deployment healthy: disk, schema, backups, extras, bind
make backup        # snapshot the DB to the internal disk, verify, prune
make test          # the table-driven suites (standalone scripts, not pytest)
make install-agents # launchd timers for discovery, dashboard and backup — mini only
make agents-stop   # unload them; do this before ejecting the SSD
```

## Conventions

- Python 3.12, stdlib-first. `sqlite3` plus a thin query module. No ORM.
- FastAPI serving a JSON API plus a React SPA. Vite 6, React 19, Tailwind 4, TypeScript,
  React Router, TanStack Query. `make build-web` on the host; `dist/` is gitignored.
- **Business logic stays in Python.** `views.py` decides what a page shows, `actions.py`
  what a button does, `queries.py` holds the SQL. React is a view layer, and the state
  vocabulary comes from `/api/meta` so it is never hardcoded in TypeScript.
- Timestamps: ISO 8601 UTC, stored as TEXT.
- Migrations: numbered `.sql` files, applied in order, never edited after they've been run.
- Secrets in `.env`. Gitignore `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `out/`.
- Scheduling: launchd plists in `~/Library/LaunchAgents`. Not cron, not systemd.
- One module per worker, matching the command names above.

## Environment gotchas

I develop on three Macs (8 GB laptop, 24 GB laptop, 24 GB mini) but **only the Mac mini runs
the app.** It holds the DB, the launchd jobs, and the dashboard. The laptops are dev clients.

- **Never put the SQLite file on iCloud Drive, Dropbox, or any sync service.** WAL mode plus
  file-level sync corrupts the database. `db.connect()` refuses such a path.
- **The DB lives on the mini's external SSD** (`/Volumes/jobhunt`, APFS, encrypted). An
  unmounted `/Volumes` path is still a writable path on the boot volume, so `db.connect()`
  checks `ismount`, then a `.jobhunt-volume` sentinel matched against `JOBHUNT_DB_VOLUME_ID`,
  then opens `mode=rw` so SQLite itself cannot create. **Only `make migrate` may create a
  database.** Everything else raises. Never add `JOBHUNT_DB` to a plist — a plist value beats
  `.env` and silently splits the agents onto a second database.
- **One writer host.** Laptops read and write through the mini's HTTP API over Tailscale,
  never by opening the DB file over a share. For local dev, use a throwaway seeded DB.
- **`docs/profile/` is untracked.** It holds my legal name, email, phone, city, pay
  expectations, full work history, and 345 other people's names and email addresses —
  third-party data that is not mine to publish. It syncs between machines with
  `make profile-push` / `make profile-pull` over the tailnet, not through git and not
  through the DB. `docs/profile.example/` is the committed template.
  Untracking does not erase history: the files are still in every commit before
  2026-08-05, so the repo stays private unless that history is purged.
- On the mini: launchd, not cron or systemd. Scheduled jobs need a logged-in GUI session, so
  an unattended reboot with no auto-login brings nothing back — and leaves the encrypted SSD
  locked as well.
- Default Mac sleep silently kills the scheduled runs:
  `sudo pmset -a sleep 0 disablesleep 1 disksleep 0 autorestart 1`.
- Never bind FastAPI beyond localhost; `serve()` raises on anything else. Cross-machine and
  phone access is **Tailscale Serve** in front of the loopback port, which also supplies the
  HTTPS the Fill helper's clipboard needs. Never `tailscale funnel` — that is the open internet
  in front of an app with no auth.
- SQLite in WAL mode, `synchronous=FULL` on the mini because an external disk can be unplugged.
  The DB holds every submitted PDF: `make backup` snapshots it to the internal disk nightly,
  verifies by reading every page, and drills a restore weekly.
- `make agents-stop` before ejecting the disk. `make doctor` answers "is this healthy".

## Watch for

- **Queue rot.** If the `job_approved` backlog grows because I'm not actually applying, cut
  the monthly target to 60–70 rather than approving jobs I won't submit.
- **Honesty drift.** Surface the `would_apply_anyway` ratio on the stats page. A falling ratio
  means the system is manufacturing volume, which is the exact thing this design exists to
  prevent.
- **Aggregator staleness.** If posted-to-discovered lag or dead-link rate climbs, add direct
  polling of Greenhouse/Lever/Ashby JSON endpoints for a seeded company list. Optional.
