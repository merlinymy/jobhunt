# Job Application System

Personal job-search pipeline. Discovers jobs, scores them against my profile, generates a
tailored resume and answer set per job, tracks outcomes. **I fill out and submit every
application by hand.** Single user, localhost only, runs on a Mac mini M4.

<!-- Detail deliberately kept out of this file to stay under the 200-line context budget.
     Read the docs below on demand; do not @import them. -->

Read on demand, not every session:

- `docs/architecture.md` — state machine, components, algorithms, packet design
- `docs/build-plan.md` — phases, gates, current status (check boxes off as work completes)
- `migrations/` — schema, source of truth
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
- **No auth, no multi-user, no Docker, no cloud deploy, no SPA.**

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
  employer, title, date, degree, or metric absent from those rows. The validator raises.
- `answers.tier = 'fact'` is returned verbatim. Never LLM-generated, never paraphrased.
- Workers are stateless and idempotent. Rerunning must not duplicate rows or re-send a
  Telegram message. Guard on `jobs.apply_url_norm` and on `events`.
- Score never advances state. It only orders the digest. I approve every job.
- `docs/profile/` is the source of truth for my profile; the DB is derived. Build a loader,
  never a CRUD editor for this data. Generated per-company narrative answers are the one
  exception — they accumulate in the DB only.

## Model routing

| Task | Model |
| --- | --- |
| Resume tailoring, narrative answers | frontier API, Opus-class |
| Job scoring pass 2, email classification | local Ollama, `qwen2.5:14b` |
| Dedup, normalization, prefilter, routing, ATS detection | no LLM — deterministic |

All frontier calls go through one wrapper module. Log prompt, response, token counts, cost,
and latency to `llm_calls` on every call.

## Commands

Intended interface — build to this contract.

```bash
make migrate       # apply migrations/*.sql in order
make load-profile  # import docs/profile/* into SQLite; idempotent, re-runnable
make dev           # dashboard on localhost:8000
make ingest        # one-shot discovery run
make score         # prefilter + LLM scoring on `discovered`
make digest        # send today's Telegram digest
make tailor        # build packets for `job_approved`
make inbox         # poll Gmail, classify, update states
make chat          # gap-filling: resolve unknown_questions, append answers
```

## Conventions

- Python 3.12, stdlib-first. `sqlite3` plus a thin query module. No ORM.
- FastAPI + Jinja + HTMX. No build step, no bundler, no JS framework.
- Timestamps: ISO 8601 UTC, stored as TEXT.
- Migrations: numbered `.sql` files, applied in order, never edited after they've been run.
- Secrets in `.env`. Gitignore `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `out/`.
- Scheduling: launchd plists in `~/Library/LaunchAgents`. Not cron, not systemd.
- One module per worker, matching the command names above.

## Environment gotchas

- macOS host: launchd, not systemd. Scheduled jobs need a logged-in GUI session.
- Default Mac sleep silently kills the 8am digest: `sudo pmset -a sleep 0 disablesleep 1`.
- Telegram uses long polling. No webhook, no port forwarding, no tunnel, no static IP.
- Never bind FastAPI beyond localhost. Phone access is via Tailscale only.
- SQLite in WAL mode. The DB holds every submitted PDF — back it up off-box nightly.
- Ollama must be running before `make score` or `make inbox`.

## Watch for

- **Queue rot.** If the `job_approved` backlog grows because I'm not actually applying, cut
  the monthly target to 60–70 rather than approving jobs I won't submit.
- **Honesty drift.** Surface the `would_apply_anyway` ratio on the stats page. A falling ratio
  means the system is manufacturing volume, which is the exact thing this design exists to
  prevent.
- **Aggregator staleness.** If posted-to-discovered lag or dead-link rate climbs, add direct
  polling of Greenhouse/Lever/Ashby JSON endpoints for a seeded company list. Optional.
