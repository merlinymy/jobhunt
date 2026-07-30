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

**Gate:** I can log an application by hand and see conversion stats.

## Phase 2 — profile store and resume tailoring

- [ ] `docs/profile/experience.yaml` filled (intake sections C, D)
- [ ] `make load-profile` imports it; re-running is idempotent
- [ ] RenderCV pipeline: master data → PDF
- [ ] Tailor prompt receives bullet rows with IDs, returns selected IDs plus reworded text
- [ ] Validator per `.claude/rules/tailoring.md`, raising on any unsourced claim
- [ ] Diff view in the dashboard showing every change against master
- [ ] Table-driven adversarial fixtures for the validator

**Gate:** Paste a JD, get a tailored PDF plus a diff — and the validator has caught at least one
deliberately fabricated claim in testing.

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
