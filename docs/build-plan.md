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

And one open question: `cv-builder` puts Skills second, right below the header. `build_cv`
currently emits it last. Section order there is insertion order, not a chosen one.

### Open — validator holes found auditing this phase

Each was reproduced against the real corpus. None is caught today.

1. **A number glued to letters can be changed silently.** `_STANDALONE_NUMBER`'s
   `(?<![A-Za-z0-9])` lookbehind stops `p99` reading as a bare `9`, but it makes the whole
   token invisible rather than comparing it. Confirmed: `v1` → `v4` accepted on bullet 68.
   Also exposed — `Y537S` (the ERα crystal structure), `BM25`, `base64`, `mulberry32`.
   Fix: extract `[A-Za-z]+\d+[A-Za-z0-9]*` from the output and require each in
   `haystack + noun_context[bid]`, the same allowance the proper-noun check uses.
2. **Lowercase invented proper nouns pass.** `at stripe`, `using terraform` — accepted. The
   tradeoff is deliberate (checking every lowercase word would flag ordinary prose), but it
   means "invented employer", a required reject, only holds when capitalized.
3. **A plain merge of two bullets under one id passes** unless it drags across a number, a
   proper noun, or a scope word. The system prompt forbids merging outright.
4. **Non-ASCII homoglyphs are invisible.** `_WORD` is `[A-Za-z]`-anchored, so `Ѕtripe` with a
   Cyrillic Ѕ tokenizes as lowercase `tripe` and is skipped. The JD is untrusted text and it
   goes into the prompt.

1 and 4 are contained fixes. 2 and 3 are design calls.

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
- [ ] Packet view renders the full answer set in likely form order, with copy buttons
- [ ] Unknown questions land in `unknown_questions` and flag on the packet

**Gate:** A packet renders a complete answer set, and a question I've never seen gets captured
rather than silently dropped.

## Phase 4 — discovery, scoring, digest

- [ ] JobSpy ingest (Indeed + Google only to start)
- [ ] URL normalization + hard and soft dedup
- [ ] `contacts` seeded from `docs/profile/contacts.csv` (LinkedIn export + hand additions)
- [ ] Pass 1 deterministic prefilter driven by `docs/profile/scoring.yaml`
- [ ] Pass 2 LLM scoring via Batch API, model from `config/models.yaml`
- [ ] Telegram digest, top ~8, inline approve/skip, referral flag surfaced
- [ ] launchd plists for ingest, score, digest

**Gate:** 8 jobs arrive in Telegram each weekday morning and ≥50% get
`would_apply_anyway = 1`. Below that, tune the prefilter — not the LLM.

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
