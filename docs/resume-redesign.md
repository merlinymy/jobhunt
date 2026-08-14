# Resume generation redesign

Status: **planned, not started.** Implementation waits for an explicit go. This
document is the durable, PII-free build plan and enforcement contract. The
source spec is `.claude/rules/resume_generation_spec.md` (to be split — see §1);
the profile *data* it seeds lives untracked in `docs/profile/`.

This file carries **no §1–§5 data** — no identity, no bullet text, no education,
no variant lines. It captures the pipeline, the schema, and the honesty gates.
The `must_keep` phrases below are short enforcement fragments keyed by opaque
bullet IDs, not personal data.

---

## 0. What this changes, in one line

The model stops **rewording** a raw corpus and starts **selecting** from a
human-approved library. Rendering pre-approved text verbatim is fabrication-proof
by construction; the validator's job shrinks to guarding the one remaining degree
of freedom (a light trim). A build drops from two model calls to one.

What does **not** change: the packet state machine, `answers_json` / `resume_pdf`
freezing, the events log, backups. The redesign is scoped to three middle steps —
select → validate → render.

---

## 1. Source of truth and the PII split

The spec was authored as one markdown file that mixed a tracked-worthy *contract*
with untracked-only *data* (phone, legal name, links). That split is now the
design:

- **Tracked contract** — spec §6 (tailoring algorithm), §7 (anti-fabrication
  flags), §8 (formatting/ATS), §9 (section rules). PII-free. Stays reviewable in
  the repo.
- **Untracked data** — spec §1 (identity, education), §2 (skills master), §3
  (summaries), §4 (bullet library), §5 (variants). Moves to
  `docs/profile/resume_library.yaml`, which is untracked like the rest of
  `docs/profile/` and synced by `profile-push` / `profile-pull`.
- **Identity** reconciles into the existing `facts.yaml` (most fields already
  present; add `work_authorization_line` and `title_default` if missing).
- **The old corpus is retired, not deleted.** `docs/profile/experience.yaml`
  (115 evidence-annotated bullets) is archived to `experience.raw.yaml`. It no
  longer feeds the pipeline, but its provenance — the source of every number —
  survives for audit. See the optional cross-link in §8.

The library is now **the coverage ceiling on tailoring quality** (§9). When a JD
wants something no bullet frames, the honest move is to add a library entry, not
to let the model reach. §9's coverage-gap note operationalizes this.

---

## 2. The selection contract

The selection model receives the JD + library + variant profiles and emits **only
the judgment-y part**:

```
{ variant, bullet_ids, trims? }        # trims: { id -> shortened text }
```

It does **not** emit free prose, a title, or a skills order. Those two fields are
the fabrication surface a "no compose" guarantee otherwise misses, so they are
**deterministic post-processing**, not model output (Amendment 2).

Verbatim library text is the default. A trim is a last resort and is heavily
guarded (Amendment 1), and a pre-authored `-short` variant is always preferred
over a runtime trim (Amendment 3).

---

## 3. Schema (additive migrations, `018`+)

Per the data-layer rules: numbered, never edited once run, constraints over
checks-in-code where possible.

- `resume_bullets` — keyed on the **string** `id` (`FBT-ship-g`), with
  `entry_key`, `claim_group`, `framing` (JSON), `tier`, `is_lead_candidate`,
  `tags` (JSON), `claim_safety`, `must_keep` (JSON), `text`. String-keyed because
  the spec makes IDs stable and referenceable; the integer-PK `bullets` table's
  positional-upsert design would fight this.
- `resume_variants` (name, title_default, summary_key, skills_order JSON),
  `resume_variant_entries` (variant, sort, kind, header fields, tech, bullet_ids
  JSON), `resume_summaries`, `resume_skill_groups`.
- `education` re-loaded to the three degrees.
- Old `bullets` / `experiences` / `projects` left **inert** — dropped in a later
  migration once nothing reads them, not in the same change that adds the new
  path.

Loader gains a `_load_library` pass with the load-time assertions in §6.

---

## 4. Amendments — honesty gates (authoritative; do not re-derive)

These close holes a naive "faithful subset / no new tokens" check leaves open. A
token-subset check blocks *additions* but not **inflation-by-omission**: a subset
can drop `of 80`, `up to`, `~`, or a negation and still pass. #1 closes that.

### Amendment 1 — `must_keep` + global denylist + clause-boundary trims

**`must_keep` per bullet** — phrases a trim may never drop.

Critical (dropping these inflates the claim):

| bullet | must_keep |
| --- | --- |
| `ARC-arch-full` | `"20-paper"`, `"evaluation corpus"`, `"blind A/B"`, `"11 of 15"` |
| `ARC-arch-short` | `"20-paper"`, `"blind A/B"`, `"11 of 15"` |
| `PEP-combined` | `"16 of 80"`, `"pilot run"` |
| `PEP-metric` | `"16 of 80"`, `"pilot run"` |
| `FPS-mp` | `"up to four players"` |

Recommended (number / scope integrity):

| bullet | must_keep |
| --- | --- |
| `JOB-cost-full`, `JOB-cost-short` | `"1,041"`, `"1,703"` |
| `FBT-scope` | `"never authorized"` |
| `FBT-rls` | `"row-level security"` |

**Global interior-token denylist** — never dropped from *any* bullet if present:

```
not · never · no · only · ~ · "up to" · "of <N>" · pilot · evaluation corpus · delivered
```

**Trims are constrained to clause boundaries** — `—`, `;`, parentheses, list
commas — **never interior function words.** A pure token-subset check permits
negation flips and qualifier drops; the clause-boundary constraint is what
prevents them.

`must_keep` is a **hard floor** (see §5 fit-loop): a bullet that cannot trim
within its `must_keep` is **keep-whole-or-drop, never mangled**, even under page
pressure.

### Amendment 2 — title and skills_order are deterministic, not model output

The selection model emitting a title is a fabrication surface: nothing stops it
returning "Senior / Staff / Lead Engineer" for an early-career candidate.

- **title** = `normalize(JD title)` through a **seniority guard**: reject
  Senior / Staff / Lead / Principal / Manager / Director unless explicitly
  whitelisted; fall back to the variant default. (Spec §6 step 3 already requires
  this — encode it as a rule, don't trust the picker.)
- **skills_order** = rank `skills_master ∩ JD`, stable-sort within groups,
  reorder groups by best member — with a **hard assertion that the result is a
  permutation/subset of `skills_master`** (no new items; the "JD skill not in
  `skills_master`" test applies here too).

Result: fewer surfaces, cheaper, and the title cannot drift.

### Amendment 3 — prefer pre-authored `-short` ids over runtime trim

For every `claim_group` that already has a short variant (`ARC.mem`, `ARC.arch`,
`JOB.cost`), select the short id under page pressure **instead of** trimming the
long one. Runtime trim is last-resort, only for bullets with no `-short`. This
leans on curation over model trimming — the stated philosophy — and shrinks the
trim guard's exposure.

### Amendment 4 — `repos_public` is strict-equality, not truthy

Print a link **iff the value `== true`.** Default-deny for `null`, `false`, and
any string. `FireProofSheep`'s value `"demo_live_leaderboard_broken"` is a truthy
string and would print a broken link under `if repos_public[p]:`.

---

## 5. Robustness (should-fix)

- **Deterministic fallback.** On selection-model error, invalid JSON, or
  validation rejection past N retries, fall back to the chosen variant's **default
  selection** (spec §5 is already a pre-approved, valid resume). A bad model turn
  never blocks a packet — matches the no-gate stance.
- **Extended load-time assertions** (library integrity is the new ceiling, so
  guard it): every `must_keep` phrase occurs **verbatim in its own bullet's text**
  (else it is dead); `tier ∈ {primary, swap, interview}`;
  `framing ⊆ {general, fullstack, backend, any}`; each variant entry has **≥1
  `is_lead_candidate` bullet available** to open with; **no variant defaults to an
  `interview`-tier id.** (These are on top of the already-planned `claim_group`
  exclusivity and id-resolution checks.)
- **The honesty backbone is cross-cutting, not renderer-only.**
  `score._profile_prefix`, `packet_chat`, and `formfill` free-text fields all
  compose candidate-claim text. Repointing them to read the library is **necessary
  but not sufficient** — if any of them generate prose (a "why this company"
  answer, a "describe your experience" field), they inherit §7: **select/quote
  numbers, never paraphrase them.** Same no-add discipline as the resume.
- **`gaps.py` must not re-emit a gap note onto the resume.** Dating is year-level
  with no note (2024 stays blank). If `gaps.py` feeds cover-letter / interview
  context, it uses the **approved narrative** (earth science → analytics →
  engineering → MS), not a computed `gap: 2024` string.
- **Fit-loop order = spec §6 step 11:** drop a `swap` bullet → select a `-short`
  variant → reduce a project to one bullet → drop the poster line. `must_keep` is
  the hard floor throughout.

---

## 6. Confirmations

- **The number check is bidirectional for trims:** emitted numbers `⊆` source (no
  new) **and** `must_keep` numbers still present.
- **Optional, cheap:** cross-link each library entry to its provenance id in
  `experience.raw.yaml`, so a future number edit is auditable. Fits the backbone;
  not a blocker.

---

## 7. Coverage-gap note (forward-looking)

Coverage is now the ceiling. Operationalize it: when selection cannot satisfy a
chunk of the JD's must-haves from the library, emit a **coverage-gap note into the
packet (not the resume)** — "JD wants X; no bullet frames it." This turns the
ceiling into a **curation backlog** instead of a silently weak resume, which is
the honest version of the trade.

---

## 8. Phases

**Phase 0 — data relocation (non-destructive).**
Create `resume_library.yaml` from spec §1–§5, folding in the Amendment 1
`must_keep` set. Reconcile identity into `facts.yaml`. Archive `experience.yaml`
→ `experience.raw.yaml`. Strip §1–§5 out of the tracked spec, leaving §6–§9 as
the contract. No code ships in this phase.

**Phase 1 — engine.**
Migrations `018`+; `_load_library` with §6 assertions; the `select` task (replaces
`tailor`'s reword call, cheaper tier viable); the repurposed validator (ids exist,
`claim_group` exclusivity, `tier != interview`, trim guard = clause-boundary +
`must_keep` + denylist + bidirectional number/identifier checks); deterministic
title + skills_order (Amendment 2); `-short`-over-trim (Amendment 3);
`repos_public == true` link gating (Amendment 4); deterministic fallback to the
variant default. Render on the existing RenderCV PDF path. New adversarial
fixtures (§10).

**Phase 2 — output.**
`python-docx` master → text-based PDF (copy-paste-in-order), `.docx`-on-request.
Isolated as its own renderer decision (`python-docx` is a new dependency) so it
does not entangle the engine.

---

## 9. Blast radius (verified)

Seven modules read the corpus tables and repoint to the library:

| module | today | after |
| --- | --- | --- |
| `tailor.py` | rewords corpus bullets | selection over the library |
| `resume.py` | corpus → RenderCV | library + variant → RenderCV |
| `web/views.py` | packet diff + `/fill` structured fields | library |
| `score.py` | `_profile_prefix` from experiences/tech | library summaries + skills (quote, never paraphrase) |
| `gaps.py` | corpus context block | library; no gap note to the resume |
| `formfill.py` | corpus context | library (quote numbers) |
| `packet_chat.py` | corpus context | library (quote numbers) |

---

## 10. Tests (adversarial fixtures)

New cases for the new gates, alongside the retained number/identifier ones:

- a trim that drops `"20-paper"` from `ARC-arch-full` → **reject** (Amendment 1)
- a trim that drops a denylist token (`up to`, `of 80`, a negation) → **reject**
- a trim that cuts at an interior function word rather than a clause boundary →
  **reject**
- two bullets from one `claim_group` on one resume → **reject**
- an `interview`-tier bullet on the resume → **reject**
- a JD skill not in `skills_master` surfaced in skills_order → **reject**
  (Amendment 2)
- a title of "Senior Engineer" for the early-career candidate → **normalized to
  the variant default** (Amendment 2)
- `repos_public` string value → **no link printed** (Amendment 4)
- selection-model garbage → **falls back to the variant default, packet still
  builds** (§5)
