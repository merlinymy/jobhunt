# Build Plan

Ordered to front-load value and defer nothing brittle. Check boxes off as work completes.
Do not start a phase before the previous phase's gate passes.

## Phase 0 — seed from real forms

Right-sized: since every application is manual, this is a head start, not a gate. The point is
that the packet has real fields to show on day one instead of guesses.

- [ ] Apply to 3–4 jobs by hand, ideally across different ATS
- [ ] Log **only the questions that surprised you** — verbatim wording
- [ ] Fill `docs/profile/facts.yaml` (sections A, H)
- [ ] Fill `docs/profile/scoring.yaml` (section B)

The recurring question set is small and converges fast. Anything missed gets caught by
`unknown_questions` later, permanently.

**Gate:** `facts.yaml` and `scoring.yaml` are filled in, and I have the wording of the
non-obvious questions.

## Phase 1 — DB and dashboard

- [ ] `migrations/001_init.sql` applied; `make migrate` works
- [ ] Thin query module over `sqlite3`; no ORM
- [ ] State-transition helper that rejects pairs absent from the transition table
- [ ] FastAPI + Jinja + HTMX dashboard on localhost
- [ ] Manual application entry form
- [ ] Stats page: conversion by ATS, by source, by referral status, `would_apply_anyway` ratio
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
