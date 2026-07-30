# Architecture

One state machine on one table. Each worker picks up rows in one state and moves them to the
next. No message broker, no Airflow, no queue — a `state` column and launchd.

## State machine

Transitions are enumerated rather than drawn — column-aligned ASCII was ambiguous about which
state each branch hangs off, and got two of them wrong.

| From | To | Trigger |
| --- | --- | --- |
| — | `discovered` | `ingest` finds a new job and it survives dedup |
| `discovered` | `filtered` | deterministic prefilter rejects it — **terminal** |
| `discovered` | `scored` | prefilter passes, LLM assigns score + reasoning |
| `scored` | `skipped` | I decline it in the digest — **terminal** |
| `scored` | `job_approved` | I approve it in the digest |
| `job_approved` | `expired` | posting gone before the packet was built — **terminal** |
| `job_approved` | `packet_ready` | `tailor` renders the resume and resolves answers |
| `packet_ready` | `expired` | posting gone before I submitted — **terminal** |
| `packet_ready` | `applied` | I tap "I applied", or a confirmation email arrives |
| `applied` | `rejected` | inbox classification or manual — **terminal** |
| `applied` | `interview` | inbox classification or manual |
| `interview` | `offer` | manual |
| `interview` | `rejected` | post-interview rejection — **terminal** |
| `offer` | — | **terminal** |

Note `applied → offer` is not a valid transition; an offer always passes through `interview`.

| State | Meaning |
| --- | --- |
| `discovered` | ingested, deduped, not yet scored |
| `scored` | has score + reasoning, awaiting my review in the digest |
| `filtered` | killed by the deterministic prefilter (retained for stats) |
| `job_approved` | I said yes in the Telegram digest |
| `skipped` | I said no |
| `packet_ready` | tailored resume + answer set generated, ready for me to submit |
| `expired` | posting disappeared before I applied |
| `applied` | I submitted it; `applied_at` set |
| `rejected` / `interview` / `offer` | from inbox classification or manual update |

Every transition writes an `events` row with `from_state` and `to_state`. The transition
helper should reject any pair not in the table above rather than trusting callers.

## Workers

| Module | Trigger | Responsibility |
| --- | --- | --- |
| `ingest` | launchd, 2×/day | JobSpy (Indeed, Google) → normalize → dedup → `discovered` |
| `score` | after ingest | deterministic prefilter, then LLM scoring on survivors (batch) |
| `digest` | launchd, 8am weekdays | Telegram: top ~8 `scored`, inline approve/skip buttons |
| `tailor` | on `job_approved` | render resume PDF + resolve answer set → `packet_ready` |
| `inbox` | launchd, hourly | Gmail API → classify → write events, close forgotten `applied` |

Only the Mac mini runs these. See Deployment below.
| `dashboard` | always on | packet view, "I applied" button, stats, manual entry |
| `load-profile` | on demand | import `docs/profile/*` into SQLite; idempotent upsert |
| `chat` | on demand | gap-filling only — resolves `unknown_questions`, appends to `answers` |

All workers are stateless. State lives in SQLite. Any worker can be killed and rerun.

## Algorithms

### URL normalization

Dedup correctness depends entirely on this. Lowercase the host, strip the trailing slash, drop
query params matching `utm_*`, `gh_src`, `ref`, `source`, `lever-source`, `src`, `trk`,
`recruiter`. Keep the path and any param that identifies the posting — verify per ATS before
dropping anything that could be a job ID rather than a source tag.

### ATS detection

Derived on read by regexing the apply URL. Never stored as a prerequisite for anything; used
only for routing decisions and stats.

```
job-boards.greenhouse.io/{slug}   boards.greenhouse.io/{slug}
jobs.lever.co/{slug}              jobs.ashbyhq.com/{slug}
apply.workable.com/{slug}         {tenant}.myworkdayjobs.com
{co}.taleo.net                    careers.smartrecruiters.com/{slug}
{co}.icims.com
```

### Scoring

Two passes.

**Pass 1, deterministic — must kill roughly 90%.** Title allowlist and blocklist, seniority
band, location and remote rules, comp floor, company blocklist, hard exclusions (clearance
required, unpaid, commission-only, staffing agencies).

**Pass 2, LLM.** Survivors get my profile summary plus the JD and return a 0–100 score with
two sentences of reasoning. Runs through the Batch API — it isn't latency-sensitive, it just
has to finish before the 8am digest. The profile summary is cached across all calls.

Score never auto-advances state. It orders the digest, nothing more.

### Answer resolution

For each field: `answers` where `company_id = X` → fall back to `company_id IS NULL` → if tier
is `narrative` and nothing is cached, generate and cache → if nothing at all, insert into
`unknown_questions` and flag it on the packet.

### Applied detection

Both paths are required. Primary: I tap "I applied" on the packet. Backstop: `inbox` matches
confirmation emails ("thank you for applying", "we received your application") to
`packet_ready` rows by company and auto-advances to `applied`. Unmarked applications silently
corrupt conversion stats, which is the entire point of instrumenting.

## The packet

The actual product. A dashboard view optimized for a manual 8–12 minute fill on a laptop.

1. Apply URL — one click, opens in a new tab
2. Tailored resume PDF — download button, plus the diff against master
3. Answer set in likely form order, each with a one-tap copy button
4. Unknown-question flags, if any
5. Referral flag — matching `contacts` at this company, with a "ping first" prompt
6. "I applied" button → sets `applied_at`, prompts for `would_apply_anyway`

Telegram handles approvals and notifications. The dashboard handles filling.

## Why no browser automation

Considered and rejected. A Workday application is 10–20 agent steps; at 95% per-step
reliability that completes roughly 40% of forms, at 99% roughly 85%. Local models sit below 95%
on long-context DOM reasoning, and frontier models make control the dominant token cost
(~15 steps × ~15k context × 100 applications/month). Against that, manual filling costs ~10
minutes and carries zero ToS exposure, zero selector maintenance, and no anti-bot arms race.
The tedium worth automating turned out to be the answer bank, not the clicking.

## Deployment

Three dev machines, one host.

- **Mac mini (24 GB)** — the only machine that runs the app. Holds `jobhunt.db`, the launchd
  timers, and the dashboard. Always on, sleep disabled.
- **Laptops (8 GB, 24 GB)** — dev clients. Clone the repo, edit code and `docs/profile/`,
  push through git. To use the app, hit the mini's dashboard over Tailscale.

Constraints that follow:

- **One writer.** The mini's process is the only thing that opens the DB file. Laptops go
  through HTTP. SQLite over a network share or a sync folder corrupts under WAL.
- **No DB in git and no DB in iCloud.** It holds BLOBs of every submitted PDF. Back it up
  off-box with a nightly copy, not by syncing the live file.
- **Profile data syncs via git**, which is the reason `docs/profile/` is committed rather than
  living only in the DB. Keep the repo private — those files contain my address, phone,
  salary expectations, and EEO answers.
- **`CLAUDE.local.md` is per-machine** and gitignored, so each Mac carries its own paths. The
  8 GB laptop should not attempt to run anything heavier than the editor.
