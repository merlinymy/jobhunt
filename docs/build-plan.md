# Build Plan

Ordered to front-load value and defer nothing brittle. Check boxes off as work completes.
Gates are about not building on a broken foundation, not about strict ordering. Phase 0 runs
in parallel throughout. Phases 1 and 2 can proceed immediately.

## Phase 0 — runs in parallel, blocks almost nothing

Not a gate. Apply to jobs because you want the applications anyway; log the questions as you
hit them. The only thing that actually waits on this is Phase 3's seed content — and even
Phase 3's _code_ can be built against a few guessed questions.

- [ ] Apply to jobs by hand, ideally across different ATS. Ongoing.
- [ ] Log **only the questions that surprised you**, verbatim, as you encounter them
- [ ] Fill the `identity` block of `docs/profile/facts.yaml` — 5 minutes, Phase 2 needs it
- [ ] Fill `docs/profile/scoring.yaml` — thinking work, ~1 hour, Phase 4 is only as good as it
- [ ] Start `docs/profile/experience.yaml` (intake section C) — **the long pole.** Several
      hours, gated on nothing, and Phase 2 stalls without it.

**Real dependencies:** Phases 1, 2, 4, and 5 need none of the applications. Phase 3 needs real
question wording for its seed data. Phase 5 benefits from real rejection emails as fixtures.

## Phase 1 — DB and dashboard

- [x] `migrations/001_init.sql` applied; `make migrate` works
- [x] Thin query module over `sqlite3`; no ORM
- [x] State-transition helper that rejects pairs absent from the transition table
- [x] FastAPI + Jinja + HTMX dashboard on localhost
- [x] Manual application entry form
- [x] Stats page: conversion by ATS, by source, by referral status, `would_apply_anyway` ratio
- [ ] Backfill the 10 Phase 0 applications
      Nothing blocks this — it is data entry. One fidelity caveat: `applied_at` backdates
      correctly, but `states.create` stamps the seed event with *now*, and the
      known-outcome walk sets `first_response_at` to the backfill moment. So
      response-latency analysis over backfilled rows will be meaningless.
      `transition()` already accepts `occurred_at`; the route just never passes it and
      the form has no response-date field. Add that first if latency matters, since
      redoing it later means deleting rows.

**Gate:** I can log an application by hand and see conversion stats.

## Phase 2 — profile store and resume tailoring

- [x] `docs/profile/experience.yaml` filled (intake sections C, D)
- [x] Migrations. Landed as four, not one — 002 was taken by the Phase 1 audit's
      `would_apply_anyway` CHECK, and the natural-key and cache-accounting needs surfaced
      after 003 was written. `003` adds the columns the corpus carries and `001_init.sql`
      lacks plus the `answers` global-default partial index; `004` carries the same
      NULL-distinctness fix up to `education` and `credentials`; `005` splits cache reads
      from writes on `llm_calls` and gives `stop_reason` its own column.
- [x] `make load-profile` imports it; re-running is idempotent
      (verified: three consecutive runs, identical counts)
- [x] RenderCV pipeline: master data → PDF — reworked onto the `engineeringresumes`
      theme after the first real render; a tailored resume now fits one page
- [x] Tailor prompt receives bullet rows with IDs, returns selected IDs plus reworded text
- [x] Validator per `.claude/rules/tailoring.md`, raising on any unsourced claim
      — with four known holes, below
- [x] Diff view in the dashboard showing every change against master
- [x] Table-driven adversarial fixtures for the validator (48 cases, all passing)

**Gate:** Paste a JD, get a tailored PDF plus a diff — and the validator has caught at least one
deliberately fabricated claim in testing.

**Gate status: mechanically met, deliberately not closed.** The path runs end to end and the
fixtures catch every fabrication they name. Two things are open, and Phase 3 builds packets on
top of both, so they are cheaper to fix now than after.

### Resume format — fixed 2026-08-03

Reworked against a real render plus the layout in `oldProjects/cv-builder`, which is the
ATS shape that has actually been used. Theme is now `engineeringresumes`, RenderCV's
engineering preset, rather than the decorative `classic`.

Fixed: the date is inline with the company instead of owning a column that squeezed every
bullet to 75% width; location is gone from the entry header; the `1 year 8 months` span is
gone; education renders as one line instead of `Master / State University, Computer
Science / of / Science`; project blurbs no longer render as unbulleted paragraphs; the
languages and posters sections are gone; and skills are capped at 22 so a tailored resume
fits one page. Verified: 10 selected bullets → 1 page. Master is 6 pages, which is correct
for a diff baseline.

Still open, both corpus data rather than renderer bugs:

- `FireProofSheep` has no `start_month` / `end_month`, so it is the one project with no date.
- It is also the only project with a `url`, so its name renders bold **and** underlined while
  the others are bold alone. Either give the others URLs or drop this one.

Section order is now an explicit list rather than insertion order, with skills leading, as in
`cv-builder`.

### Open — validator holes found auditing this phase

Each was reproduced against the real corpus. None is caught today.

1. ~~**A number glued to letters can be changed silently.**~~ **Fixed 2026-08-03.** Glued
   tokens are now compared whole, against the bullet's own row rather than the widened
   context the name checks use — a digit inside a name is still a number, and rule 2 pins
   numbers to their own bullet. That distinction decided the real case: a tech list holding
   `Tailwind CSS v4` was enough to license rewording `since the v1 launch` into `v4`.
2. **Lowercase invented proper nouns pass.** `at stripe`, `using terraform` — accepted. The
   tradeoff is deliberate (checking every lowercase word would flag ordinary prose), but it
   means "invented employer", a required reject, only holds when capitalized. **Open — this
   is a design call, not a bug.**
3. **A plain merge of two bullets under one id passes** unless it drags across a number, a
   proper noun, or a scope word. The system prompt forbids merging outright. A content-overlap
   check would catch it but risks rejecting heavy legitimate rewording. **Open — design call.**
4. ~~**Non-ASCII homoglyphs are invisible.**~~ **Fixed 2026-08-03.** A non-ASCII letter the
   source never uses is now rejected. Not an ASCII-only rule — `ERα` is in the corpus, so the
   test is whether the source uses that character at all.

Fixture count is 55, up from 48; all 102 corpus bullets still validate verbatim.

### Also noted

- `verify_schema` passes on a DB that only ran `001`, so `/tailor` then fails with
  `no such table: languages` instead of saying to migrate.
- `llm_calls.prompt` stores only the varying part, not the cached corpus prefix, so a call
  cannot be reconstructed from its row. CLAUDE.md says to log the prompt — decide which.
- `peptideDesign` sits under `projects:`, not `experiences:`. An earlier decision was to move
  it, which would give two work entries instead of one. Confirm which is current.

## Phase 3 — answer bank and chat intake

- [ ] `answers` populated for every field observed in Phase 0
- [ ] `docs/profile/stories.md` written (intake sections E, F), loaded into `stories`
- [ ] `chat` worker: walks intake sections, writes to DB, appends new answers
- [ ] Answer resolution chain (company override → global → generate → unknown)
- [~] Packet view. `/packet/{id}` exists: apply link, tailored PDF served from the stored
      bytes, diff against master, referral flag, "I applied". Structured fields (identity,
      work history, projects, education) are on `/fill` with copy buttons. **What is missing
      is the free-text answer set** — the narrative questions — which needs the answer bank.
- [ ] Unknown questions land in `unknown_questions` and flag on the packet

**Gate:** A packet renders a complete answer set, and a question I've never seen gets captured
rather than silently dropped.

## Phase 4 — discovery, scoring, review queue

- [x] JobSpy ingest — **Indeed only. Google is dead.** Measured 2026-08-03 against
      python-jobspy 1.1.82: of the four US sources it exposes, `indeed` returned rows and
      `google`, `zip_recruiter` and `glassdoor` each returned zero on every phrasing tried.
      Google fails with "initial cursor not found". So discovery has one source, which is a
      single point of failure — if Indeed follows, CLAUDE.md's "Aggregator staleness" note
      (polling Greenhouse/Lever/Ashby JSON for a seeded company list) stops being optional.
      `make ingest` prints the count of searches that returned nothing, every run, so a
      source dying quietly is visible rather than looking like a slow week.
      Verified end to end: 36 searches, 1210 postings, 528 new, rerun inserted 0.
- [x] Direct ATS board polling — Greenhouse, Lever, Ashby, for a seeded company list.
      52 boards, 8,735 postings in ~70s, of which 4,864 pass the title rule. Seeded from
      the Indeed sweep itself: 28 Greenhouse boards recovered by following `grnh.se`
      short links. Throttled per host (1 req/s, jittered, `Retry-After` honoured, 429
      abandons that vendor for the run). This is the durable source — vendors' own JSON,
      no markup to rot.
- [x] URL normalization + hard dedup. Soft dedup is **not** done — see below.
- [x] `contacts` seeded from the LinkedIn export + hand additions. Loaded by
      `make load-profile`, which reads both `Connections.csv` (LinkedIn's own filename)
      and `contacts.csv` (the hand-written schema, which carries `relationship` and
      `do_not_contact` — the two things LinkedIn cannot supply). 345 rows → 246 contacts,
      99 skipped where LinkedIn withholds the name, 211 companies created.

      **Only 1 of 59 seeded boards is a company where a contact works.** A referral is
      the biggest single lever on interview rate, so that number is worth raising — but
      not automatically: probing guessed Greenhouse/Lever/Ashby slugs for the 14
      best-connected companies found **zero** public boards, so the network sits at
      companies that either do not use those three or are not hiring publicly. Adding
      board slugs by hand for companies you actually know someone at is the move.
- [x] Pass 1 deterministic prefilter driven by `docs/profile/scoring.yaml` — `prefilter.py`.
      4,982 real rows → 3,510 in 1.9s. Every check fails open; each rejection records the
      rule that fired in its `events` row.
- [x] Pass 2 LLM scoring via Batch API, model from `config/models.yaml` — `score.py`.
      ~$3 for the whole backlog at Haiku batch rates, and one-time: dedup means a posting
      is scored once ever. **The live API call is unverified** — needs `ANTHROPIC_API_KEY`.
- [x] Review queue at `/review`, top ~8, approve/skip, referral flag surfaced.
      Ordered by location tier then score. **Replaced the Telegram digest** — one surface
      instead of two, and a pull queue needs none of the send-once machinery a push
      notification did: no bot token, no long polling, no once-per-day guard, nothing to
      configure before it works.
- [ ] launchd plists for ingest and score

**Gate:** 8 jobs arrive in Telegram each weekday morning and ≥50% get
`would_apply_anyway = 1`. Below that, tune the prefilter — not the LLM.

### Open — soft dedup

Hard dedup works: `apply_url_norm` is UNIQUE and a rerun of the full sweep inserted zero.
Within one sweep it caught 638 repeats out of 1210. What it cannot catch is one posting
reachable by two different URLs, and a real sweep produced three shapes of that:

- **Aggregator redirect wrappers**, 29 of 553. `click.appcast.io`, `jsv3.recruitics.com`,
  `rr.jobsyn.org`, `dsp.prng.co`, `tnl2.jometer.com` — the real URL is behind an opaque
  token, so the same job reached directly gets a second row. `normalize.is_redirect_wrapper`
  identifies them but nothing acts on it yet. Recruitics is the one worth unwrapping: it
  carries the destination in its own `rx_url` query param.
- **Same job, cosmetically different paths.** `capgemini.com/jobs/525995-en_US+sap_btp` and
  `.../525995-en_US_SAPBTP/`; `careers.oxfordeconomics.com/en/postings/<uuid>` and the same
  UUID without the `/en/`.
- **Indeed fallback.** 18 of 553 had no `job_url_direct`, so the key is an `indeed.com/job/`
  URL. If the same posting later arrives with its ATS URL, that is two rows.

Cheapest useful version is a soft key on `(company_id, title_norm)` surfaced as a warning
rather than a merge — deciding which of two rows survives is a judgement call, and the whole
point of `apply_url_norm` being a constraint is that nothing silently discards a shot.

## Phase 5 — inbox poller

- [ ] Gmail API OAuth flow completed, `credentials.json` in place
- [ ] LLM classification: rejection / interview / confirmation / noise
- [ ] Events written, states advanced, `first_response_at` set
- [ ] Confirmation-email backstop closes forgotten `applied` rows
- [ ] launchd plist, hourly

**Gate:** Rejections move themselves without me touching the dashboard.

## Later — interview coach

Reuses `profile_facts`, `stories`, the JD, and the submitted resume.

- [ ] Question generation from a specific JD plus my story bank
- [ ] Practice sessions with feedback against `stories`
- [ ] Optional voice loop

**Constraint:** practice only. Nothing that assists during a live interview.
