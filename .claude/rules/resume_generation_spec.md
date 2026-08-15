# Resume Generation Spec

Reference for the resume-generation component of the `jobhunt` project. Consumed by a Claude Code agent that, given a job description (JD), assembles a one-page ATS-safe resume from a fixed library of pre-written, human-approved content.

## How to use this file
- **Nothing here may be paraphrased into new claims.** The bullet library below is the *only* source of accomplishment text. The agent selects, orders, and lightly trims bullets — it does not invent or embellish. This spec is the input contract; the existing 9-rule fabrication validator is the enforcement gate. Every emitted bullet must (a) come from this library and (b) pass the validator.
- The agent's job per JD: pick a **variant**, set the **title**, **reorder skills**, **select/order bullets**, enforce **formatting + length**, then validate.
- IDs are stable. Reference bullets by `id`. Bullets sharing a `claim_group` are **mutually exclusive alternatives** (different framings of the same underlying fact) — pick at most one per group per resume.

---

## 6. Tailoring algorithm (run per JD)

1. **Parse the JD.** Extract: `title`, `seniority`, required `hard_skills` (languages/frameworks/DBs/cloud/tools), `methodologies`, `domain`, and any explicit resume-format instruction (e.g., "submit as .docx").
2. **Pick the variant.** Score JD hard-skills against variant tag profiles:
   - Frontend/React/UI/design/product-heavy → `fullstack`.
   - Backend/platform/infrastructure/distributed-systems/data/API/reliability-heavy → `backend`.
   - Mixed or generic "Software Engineer / SWE / SDE" → `general`.
3. **Set the title line** to the JD's title, normalized and **truthful**. Do not claim a seniority the candidate can't support; if the JD title implies seniority beyond early-career, fall back to the variant's default title. Never use gimmicky titles.
4. **Set the summary** = variant `summary_key`. Optionally adjust only the first clause to name the JD's domain (e.g., swap "backend systems and AI/LLM integration" ordering) — but only using phrasing already true of the candidate.
5. **Reorder skills.** Start from the variant `skills_order`. Move any group and any item that the JD lists as must-have to the front. Ensure every JD-named tool that exists in `skills_master` is present and surfaced early. **Never add a tool not in `skills_master`.** Spell out acronyms + acronym once. Drop the `ML / Scientific` group unless a `PEP.*` bullet is on the resume.
6. **Select bullets** per entry, honoring `claim_group` exclusivity:
   - For each role/project, pick bullets whose `tags`/`framing` match the JD emphasis. Prefer an `is_lead_candidate` bullet as the first line of each entry.
   - Prefer `tier: primary`; pull a `tier: swap` bullet in only when it matches a JD must-have better than a primary. Never place `tier: interview` bullets on the resume.
   - **Max bullets:** jobs 3–4; projects 1–2. Most recent role gets the most bullets.
7. **Select projects.** Default `ARC.*` + `JOB.*`. Swap `PEP.*` in (replacing the weaker of `ARC.*`/`JOB.*` for that JD) for research/ML/AI/comp-bio roles; swap `FPS.*` in for frontend/game/real-time roles. **Max 2 projects** on a one-pager with two jobs.
8. **Order sections:** Header → Summary → Education → Technical Skills → Experience (reverse chronological) → Projects. (Projects-before-Experience only if a JD makes the projects clearly stronger than both roles — rare now that there are two real jobs; default Experience first.)
9. **Dates:** experience year-level (`2022 – 2023`, `2025 – Present`) to keep the 2024 gap invisible; education year-only. **Never fabricate a date.** Use only confirmed dates (see `unconfirmed`).
10. **Links:** always print LinkedIn, GitHub, website in the header. Print a project repo/demo link **only if `repos_public` says it is public and working.** (Currently: none of the project links are safe to print except the `FPS.*` project's demo, and only with awareness its leaderboard is broken.)
11. **Enforce formatting + length** (§8). If over one page, trim in this order: drop a `swap` bullet → shorten a long primary bullet to its `-short` variant → reduce a project to 1 bullet → drop the optional poster line. Do not shrink font below 10pt or drop a whole role.
12. **Validate.** Run every emitted bullet through the fabrication validator and the §7 constraints. Reject and re-select on any violation. Emit `.docx` master + text-based PDF; obey any JD format instruction.

### Per-application quick-tailor (the 5-minute path, when not regenerating from scratch)
- Change the title line to the posting's title.
- Move the posting's must-have skills to the front of the skills line; confirm each named tool the candidate has is present.
- Swap at most 1–2 bullets to matching `swap` bullets.
- Re-validate. Done.

---

## 7. Anti-fabrication & honesty constraints (HARD — the generator must satisfy these; the validator enforces them)

Global rule (mirrors the existing 9-rule validator): a bullet may be **reworded, reordered, shortened, or have its framing swapped**, but must **never introduce an employer, job title, date, number, proper noun, tool, or scope that is not present in the source bullet/library**, and must **never reword shared/team work as solo**.

Claim-specific hard flags, keyed by `claim_group` / bullet `id`. The data these
operate on lives in the untracked library's `claim_safety` + guard fields; this
section is the mechanism, and each flag names the guard that enforces it.

1. **`FBT.ship`** — the increase is client-reported analytics, *observed not instrumented*. Keep "saw/drove a 200% increase"; never "measured," "A/B tested," or "attributed." Guard: `must_keep: 200%` + `no_upgrade`.
2. **`FBT.scope`** — direct auto-publishing was **never shipped**; copy-to-clipboard was the hand-off. No bullet may imply the tool auto-posts. Guard: `must_keep: never authorized`.
3. **`FBT.backend`** — exactly five endpoints; do not inflate the count. Guard: `must_keep: five`.
4. **`ARC.arch`** — the head-to-head result is a blind A/B over a **separate 20-paper evaluation corpus**, which must be stated; never attach it to the real library or to the `ARC.evalset` 50-query set, which is "built," never "scored." Guard: `must_keep` the corpus phrases + `no_add: scored` on `ARC.evalset`.
5. **`ARC.*` scale** — one researcher, self-hosted; depth, not production scale. No SLA/uptime/many-users claims.
6. **`JOB.validator`** — **zero applications have ever been submitted.** Never claim applications sent, interviews, callbacks, or offers; describe discovery/scoring/tailoring only. Guard: `no_add: applications, interviews, offers, callbacks`.
7. **`JOB.*` numbers** — 1,041 / 1,703 / 662 / $0.73 / 19 of 40 / 2.86 MB→522 KB / 1,334 / 24 / 11 states / nine rules are verbatim; no rounding, no invented deltas. Guard: `must_keep` on the load-bearing figures; the number check blocks any new number.
8. **`PEP.pipeline`** — an unpaid project, not employment. "16 of 80" = designs passing published accept criteria in a **pilot run**; not discovered binders, not wet-lab validated. **`PEP.groove` was delivered but never exercised** — never imply use. Guard: `must_keep`/`no_add` on those bullets.
9. **`FPS.multiplayer`** — proof of concept; leaderboard currently broken; being rebuilt. No working-leaderboard or live-production claims; "up to four players" = configured cap. Guard: `must_keep: up to four players` + `no_add: working leaderboard, production`.
10. **`SIR.migrate` / `SIR.subjectline`** — the ML models were inherited; the work was the full-stack/migration/deployment layer *around* them. Never imply the candidate built the models. Guard: `no_add: built/designed the model(s)`.
11. **Dates & the gap** — never fabricate employment to fill the 2024 gap. Year-level dating is the only treatment; the resume carries no gap note. Use only confirmed dates (`open_facts.unconfirmed`).
12. **Names/links** — from `facts.yaml` (`display_name`; `legal_name` only where legally required). Never print a repo link that `repos_public` does not mark public, or a broken demo, without flagging.

---

## 8. Formatting / ATS rules (layout contract for the renderer)

- **One page.** Single column. No tables, multi-column layouts, text boxes, images, icons, logos, or skill-rating bars.
- **Standard section headings**, literal: `Summary`, `Education`, `Technical Skills`, `Experience`, `Projects`.
- **Fonts:** Calibri / Arial / Garamond; body 10–11pt (never below 10); name ~20pt; secondary text a muted gray is fine.
- **Bullets:** real bullet character via list formatting — never a literal `•` typed into a text run.
- **Contact info in the body**, never in the page header/footer region (some parsers ignore those regions).
- **Dates right-aligned via a right tab stop**, not spaces. Format consistent (year-level for experience; year-only for education).
- **Margins:** ~0.5–0.6".
- **Header must include** the work-authorization line, LinkedIn, GitHub, and website.
- **Bullet style:** verb-led, 1–2 lines, XYZ shape (action + technology + scope owned + quantified result).
- **Output:** keep a `.docx` master; submit a **text-based PDF** (must pass copy-paste-in-order test). If the posting explicitly asks for `.docx`, submit `.docx`.
- **File name:** the header display name from `facts.yaml`, spaces → underscores, as `<Display_Name>_Resume.pdf` (optionally `<Display_Name>_<Role>.pdf`).

---

## 9. Section-level rules

- **Experience:** reverse chronological — the more recent role before the earlier one. Job entries carry 3–4 bullets; the lead bullet is the strongest, quantified, JD-matching one.
- **Projects:** 2 max alongside two jobs. Each: name (+ link only if public/working), one-line descriptor, a tech line, 1–2 bullets.
- **Education:** three degrees, reverse chronological, year-only, no coursework/GPA by default. Add relevant coursework only if targeting programs that value it *and* there's room. Add GPA only if ≥3.5 and supplied.
- **Skills:** grouped; JD must-haves first; only backed skills.

---

## 10. Known gaps / improvement hooks (for the agent to surface to the user, not to fabricate around)

- Making either suppressed-link project public (`repos_public` currently blocks both defaults) would unlock a clickable link — the single highest-value change to hit-rate.
- Confirm the earlier role's inferred end date and omitted location (see `open_facts.unconfirmed`).
- All current bullets lean **single-user / solo**; a real multi-user deployment or a merged contribution to a well-known open-source repo would add the scale/team signal the library currently can't supply. If/when that exists, add it as a new library entry with its own `claim_safety`.
