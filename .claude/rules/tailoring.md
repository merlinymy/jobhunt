---
paths:
  - "**/tailor*.py"
  - "**/prompts/**"
  - "**/resume/**"
---

# Resume tailoring rules

The no-fabrication guarantee rests entirely on this code. There is no test suite elsewhere in
the project; these rails are the substitute.

## What the tailor may and may not do

The prompt receives `bullets` rows including their IDs, metrics, and `solo` flag. It returns
selected bullet IDs plus reworded text for each.

**Permitted:** selecting a subset, reordering, rewording, adjusting emphasis, changing which
skills are foregrounded, tightening for length.

**Forbidden, without exception:** introducing an employer, job title, date, degree,
certification, or metric that does not appear in the source rows. Inflating a number.
Shifting a date. Converting shared credit (`solo = 0`) into individual credit. Merging two
bullets in a way that implies a scope neither had.

## Validator

Two of them, running after every tailor call and before the PDF is rendered. Both raise;
neither warns, and neither falls back to the untailored resume silently.

**`validate()` — arithmetic, in code.** Every number, spelled number and glued identifier in
the output must appear in its own source row; `p99` is not `p95` and `v1` is not `v4`. Plus
the structural checks: ids map to real rows, no row used twice, no homoglyphs. This stays in
Python because it is exact string comparison — asked whether `18%` appears in a source that
says `15%`, a model will sometimes say yes, and a shifted percentile is invisible in a diff.

**`review()` — reading, on a model.** An invented employer, a category word standing in for a
specific one ("an LLM" for Claude), scope widened, credit taken, seniority implied, causation
claimed. Runs on `tailor_check` in models.yaml, which is deliberately a *different model* from
`tailor`: two calls to one model share its blind spots. It is given the source row and the
emitted line and **not the job description** — a checker that knows what job is being applied
for can be talked into a claim by how well it fits.

This replaced ~230 lines of regex that tried to infer word class and voice. Those produced
every false rejection the system ever made, including refusing the word `Stopped`.

Checks, at minimum, and which half owns each:

1. Every emitted bullet maps to a real `bullets.id` from the input set — *code*
2. Every number in the output appears in the source `text` or `metric` of its own bullet,
   digits or spelled out, identifiers compared whole — *code*
3. Employer names, titles, and date ranges match `experiences` exactly — *model*
4. Degrees and schools match `education` exactly — *model*
5. No bullet from `solo = 0` is reworded into individual credit, including the forms that
   use no first-person pronoun — "Led the migration" takes sole credit as squarely as
   "I migrated" — *model*
6. Scope not widened, seniority not implied, causation not claimed — *model*
7. The summary is checked the same way, against the whole corpus rather than one row —
   it is the easiest place on a resume to pick up a seniority label or a round number of
   years that nothing supports — *both halves*

Rules 3-6 were regex once, and each was a heuristic standing in for a reading. Scope widening
was a wordlist of "entire", "every", "company-wide"; sole credit was a list of team verbs;
proper nouns were "is this word capitalized". All three fired on ordinary English and none of
them ever caught a fabrication. They are checks a reader makes, so a reader makes them.

Adversarial fixtures live alongside the validator and must include: an invented employer, a
shifted end date, an inflated percentage, a fabricated degree, a shared-credit bullet
rewritten as solo, and — for the summary — invented years of experience. All must be rejected.

They are split to match. Arithmetic cases run offline in `make test`. The reading cases run
under `make test-live`, which calls the model and costs cents — that run is the only evidence
the delegated half works, and it scores legitimate cases too, because a checker that rejects
everything passes the fabrication half perfectly and is useless. On its first run it caught
all 17 fabrications and flagged three cases this file had called legitimate; two of those were
this file being wrong.

## Diff surfacing

Every tailored resume gets a diff against master rendered in the dashboard before I use it.
Reworded bullets show old and new side by side. If a diff cannot be produced, treat that as a
failure and block the packet.

## Output format

Single column. Standard section headings. No tables, no text boxes, no multi-column layouts,
no contact info in headers or footers — Workday and Taleo parsers mishandle all of these.
Dates as `Jan 2023 – Mar 2025`. Real selectable text, never rasterized.
