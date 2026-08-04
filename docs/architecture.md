# Architecture

One state machine on one table. Each worker picks up rows in one state and moves them to the
next. No message broker, no Airflow, no queue — a `state` column and launchd.

## State machine

Transitions are enumerated rather than drawn — column-aligned ASCII was ambiguous about which
state each branch hangs off, and got two of them wrong.

| From | To | Trigger |
| --- | --- | --- |
| — | `discovered` | `ingest` finds a new job and it survives dedup |
| — | `applied` | manual entry of an application I already submitted by hand. It never passed through discovery, so it seeds one honest event rather than a fabricated history |
| `discovered` | `filtered` | deterministic prefilter rejects it — **terminal** |
| `discovered` | `scored` | prefilter passes, LLM assigns score + reasoning |
| `scored` | `skipped` | I decline it on `/review` — **terminal** |
| `scored` | `job_approved` | I approve it on `/review` |
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
| `scored` | has score + reasoning, waiting in the `/review` queue |
| `filtered` | killed by the deterministic prefilter (retained for stats) |
| `job_approved` | I said yes on `/review` |
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
| `ingest` | launchd, 2×/day | JobSpy (Indeed) + direct ATS board polls → normalize → dedup → `discovered` |
| `score` | after ingest | deterministic prefilter, then LLM scoring on survivors (batch) |
| `tailor` | on `job_approved` | render resume PDF + resolve answer set → `packet_ready` |
| `inbox` | launchd, hourly | Gmail API → classify → write events, close forgotten `applied` |
| `dashboard` | always on | review queue, packet view, fill helper, "I applied", stats |
| `load-profile` | on demand | import `docs/profile/*` into SQLite; idempotent upsert |
| `chat` | on demand | gap-filling only — resolves `unknown_questions`, appends to `answers` |

Only the Mac mini runs these. See Deployment below.

All workers are stateless. State lives in SQLite. Any worker can be killed and rerun.

### Discovery has two sources, on purpose

`ingest` runs both, and they fail in different ways.

**JobSpy against Indeed.** Works today; it is a scrape, and it is the only source
that can get us banned. Of the four US sources JobSpy exposes, Indeed is the only one
that returns anything — Google, ZipRecruiter and Glassdoor all returned zero when
measured on 2026-08-03. Google is not coming back: a plain HTTP request to
`google.com/search` now returns a JavaScript gate rather than results, for any query,
with or without `udm=8`, and the only fix is executing JS. That is browser automation,
which is a hard non-goal. So Indeed is paced at 8s with jitter, and the sweep aborts
itself after four consecutive empty searches — the scraper reports throttling as zero
rows, not as an error.

**Direct ATS board polls.** Greenhouse, Lever, and Ashby's own public JSON, for a
hand-seeded company list in `config/searches.yaml`. Documented endpoints meant for
machine reads, so there is no markup to rot and no bot detection. Throttled per host
in `boards.py`, not per run. This is what CLAUDE.md's "aggregator staleness" note
proposed as optional; it stopped being optional when Google went JS-only and discovery
had a single point of failure.

The unit of work differs, and that is the real reason to keep both. A keyword search
can miss a posting whose title used a word I did not think of; a board poll returns the
entire company but only for companies I already know to watch.

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

**Pass 1, deterministic — rejects on title and level, and little else.** Title keyword match
plus blocklist, seniority band, company blocklist, tech dealbreakers, hard exclusions (clearance
required, unpaid, commission-only, staffing agencies).

Titles are matched by keyword (`title_keywords`, any substring, case-insensitive) rather than by
an exact-string allowlist. Real titles vary far too much — an allowlist would have to enumerate
"Software Development Engineer II", "Senior Full Stack Developer", and dozens more, and it fails
*silently* when it misses one. Keywords trade precision for recall on purpose: noise like "Sales
Engineer" reaches Pass 2 and gets scored down, which is the cheap direction to be wrong in.

Keywords are not a complete solution either — a title sharing no word with the list still
drops, "Member of Technical Staff" being the obvious case. That's a known gap, closed by adding
a keyword when a real posting reveals it, not by pretending the matcher is exhaustive.

Discipline (frontend / backend / full-stack) is deliberately not filtered: that's fit, which
Pass 2 scores and I judge on `/review`.

**The old "must kill roughly 90%" target is void.** It was written when comp and location were
both filters. Neither is now — title and level are what I actually reject on, so Pass 1 kills
far less and more postings reach Pass 2. That is the intended trade: the objective is
interviews per hand-submitted application, and discarding a posting on weak evidence costs a
shot for nothing. Re-baseline the kill rate against real ingest volume rather than tuning
toward a number this doc guessed.

Consequence worth watching: more survivors means more Pass 2 calls. Haiku, batched, with the
profile corpus cached keeps that cheap, but `llm_calls` is where to confirm it rather than
assume it.

**Comp is deliberately not a filter.** Most postings state no range at all, and disclosure
tracks state pay-transparency law rather than job quality — so a comp floor under-samples
non-mandating states for reasons unrelated to the roles. `facts.yaml` `comp:` holds one
pre-decided number, `walk_away`, which tells me when to leave a conversation and never rejects
a posting. The figure I actually give an employer is decided at packet time and stored per
company (see Answer resolution). Where a posting does state a range, `/review` surfaces it and
I skip it myself in one tap.

**Pass 2, LLM.** Survivors get my profile summary plus the JD and return a 0–100 score with
two sentences of reasoning. Runs through the Batch API — it isn't latency-sensitive, it just
has to finish before I sit down to review. The profile summary is cached across all calls.

**Location ranks, it does not filter.** `scoring.yaml` `location_rank` is an ordered list
ending in a catch-all, so nothing is rejected for where it is. `/review` sorts by location
tier first, then by score within a tier. That order is deterministic on purpose: I stated the
preference explicitly, so an LLM has no business re-deriving it. The LLM scores fit; the
config decides which tier a posting sits in.

Score never auto-advances state. It orders the review queue, nothing more.

### Answer resolution

For each field: `answers` where `company_id = X` → fall back to `company_id IS NULL` → if tier
is `narrative` and nothing is cached, generate and cache → if nothing at all, insert into
`unknown_questions` and flag it on the packet.

**Comp is a fact-tier answer scoped to a company.** There is no global default, because the
number depends on the job's market. The first packet for a company prompts me for the figure —
with market research alongside it, once that exists — and stores my answer at
`company_id = X`. Every later application to that company reuses it verbatim, so I never quote
two different numbers to one employer. It stays fact tier: I type it, the system never
generates it.

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

The dashboard is the whole interface: review and approve, tailor, fill, track.

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
  timers, and the dashboard. Always on, sleep disabled. Agents live in `deploy/` and are
  installed with `make install-agents`, which refuses to proceed without confirming this is
  the mini.
- **Laptops (8 GB, 24 GB)** — dev clients. Clone the repo, edit code and `docs/profile/`,
  push through git. To use the app, hit the mini's dashboard over Tailscale.

Constraints that follow:

- **One writer.** The mini's process is the only thing that opens the DB file. Laptops go
  through HTTP. SQLite over a network share or a sync folder corrupts under WAL.
- **No DB in git and no DB in iCloud.** It holds BLOBs of every submitted PDF. Back it up
  off-box with a nightly copy, not by syncing the live file.
- **Profile data syncs via git**, which is the reason `docs/profile/` is committed rather than
  living only in the DB. Keep the repo private — those files contain my legal name, email,
  phone, city, pay expectations, full work history, and a contact network with other people's
  names in it. Street address and EEO answers are deliberately not collected.
- **`CLAUDE.local.md` is per-machine** and gitignored, so each Mac carries its own paths. The
  8 GB laptop should not attempt to run anything heavier than the editor.
