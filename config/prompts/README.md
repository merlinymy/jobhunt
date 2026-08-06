# Task system prompts

One file per task, named for the task key in `../models.yaml`. **The whole file is the
prompt** — there is no comment syntax and no frontmatter, so anything you write here goes
to the model. Notes about a prompt belong in this README or in the commit message.

`llm.py` re-reads the file on every call. Edit, save, hit the button again — no restart,
even with the dashboard running. The cost is one 4 KB read against a call that takes
seconds.

The sha of whatever was sent lands in `llm_calls.system_sha`, so a run can be attributed
to a prompt revision after the fact. `python -m jobhunt.llm` prints the current shas.

```
tailor.md        builds one tailored resume    -> tailor.py
tailor_check.md  reads it back against source  -> tailor.review()
score.md         ranks one posting 0-100       -> score.py  (sync and batch)
narrative.md     drafts one application answer -> answers.py
```

A task routed in `models.yaml` with no file here fails at call time with the path it
looked for. `classify_email` is deliberately absent until Phase 5 gives it a call site.

## What the code depends on

These prompts are not free text. Each one has a contract with a parser or a validator,
and breaking it fails the whole call rather than degrading quietly.

**`tailor.md`** is the tightly coupled one. `tailor.py` validates every returned bullet
against its source row and raises `FabricationError` on any violation — the model gets
exactly one retry, with the validator's own complaint handed back, and then the packet
build fails.

This prompt used to be forty-four lines, most of them telling the model how to stay inside
the validator. That is what made the output read like it was written for a linter, so it is
now one paragraph of judgement plus one of constraint, and the model decides what a good
resume looks like. The scope-magnifier check was deleted to match. What is still load
bearing, and will cost you rejections if you drop it:

- the JSON envelope `{"reasoning": ..., "summary": ..., "bullets": [{"id": ..., "text": ...}]}`
- *each line opens with a verb* — a capitalized first word reads as a proper-noun claim.
  Kept because it is also ordinary resume advice, not only because the validator wants it
- *use the corpus's own vocabulary* — "an LLM" for a source that says Claude is unsourced
- *never change a number, or add one the source lacks*
- *one output bullet per source row*, or the id mapping breaks
- *keep `shared: true` work visibly shared*
- the summary rule — it is checked corpus-wide by `validate_summary`, so "senior" and "six
  years" are rejected unless the profile says them

Everything else is yours to change: which bullets to favour, what to lead with, how much
latitude to give.

One measured lesson, from `llm_calls` rather than taste. The first one-paragraph revision
dropped the old prompt's *"roughly 25 words"* along with the linter-appeasement, on the
theory that the model should decide. It went the wrong way — median bullet length rose from
32 words to 35 with a maximum of 50, against source bullets whose own median is 31. Without a
number the model reprints the source. Length is craft guidance, not a validator rule, and it
has to be concrete to be followed.

The second lesson, same shape. The validator can only tell an invented number from a sourced
one; it has no opinion on whether a sourced number is worth printing. Ten of the 117 corpus
bullets carry volume counts — lines of code, routes, files — and the tailor was faithfully
reprinting them, along with one sub-$10 total that made the work sound smaller than it is.
Which numbers earn a line is a judgement, so it lives in the prompt: keep scale, change and
tradeoffs; drop volume; prefer the ratio to a small absolute. Dropping is always permitted,
so none of this needed a validator change.

The third revision applied published resume guidance rather than taste. What survived the
reading, because it was consistent across sources and actionable in a prompt:

- **Result before method.** Google's XYZ formula — "Accomplished X as measured by Y by doing
  Z" — puts the outcome at the left edge, where a top-down scan lands. The prompt had it
  backwards ("what was built or changed and what it produced"), which buries the result
  behind the implementation.
- **Three to five lines per entry, strongest first.** The first three bullets of the most
  recent entry are frequently all that is read. Bullet order is now load-bearing twice over:
  `resume.build_cv` also reads the first pick's parent to decide whether experience or
  projects leads the page.
- **The swap test.** A line that could sit unchanged on another candidate's resume is
  describing a job rather than this person's work.
- **No "responsible for" / "helped with".**

Deliberately *not* applied: the common advice to match a posting's keywords exactly ("React.js"
not "React"). It collides with the validator — a name the corpus does not contain is an
unsourced claim and the whole answer is rejected. `resume._tech_union` already does the safe
version, ranking the Skills section against the JD.

One caveat on the worked example in the prompt: it contains a number, and a model that copies
it verbatim will be rejected and retried. The example is worth the risk because number
*placement* is the thing being taught, and the retry is the safety net that already exists.

The fourth revision did the same reading for the **summary**, which until then was the one part
of the page with no craft guidance at all: a single sentence of pure constraint, telling the
model what it may not write and nothing about what a good one looks like. The observed output
was what that predicts — *"Full-stack developer who ships React + TypeScript front ends over
REST APIs and Postgres … Comfortable with Docker Compose and with debugging concurrency and
memory faults under real load."* A filler opening, a laundry-list tail, and one claim the
checker caught as unsourced.

What was consistent across sources and is now in the prompt:

- **Lead with domain, not a language list.** The advice is a positioning statement, not an
  inventory — the Skills section already carries the inventory, and a list of languages reads
  identically on every other candidate's page.
- **One concrete result, and specifically one already on the page.** "Start with proof" was the
  most repeated line in the reading. The addition here is *taken from a line you actually kept*,
  so the summary and the bullets underneath argue one case instead of two.
- **The swap test, applied hardest here.** Already in the prompt for bullets. The summary is
  where a generic three lines is most tempting, so it is repeated rather than assumed.
- **Named anti-patterns**, because a phrase the model can pattern-match beats an abstraction:
  "passionate developer with a proven track record", "comfortable with X", "worked with".
  "Comfortable with" is on that list because it is what the observed bad summary actually said.
- **Let the posting choose the thread, the corpus choose the words.** The safe half of "tailor
  to the JD", now stated in the paragraph where it is tempted rather than only in this README.

Deliberately *not* applied, all three near-universal in the published guidance:

- **Years of experience and seniority level.** Nearly every source opens its template with
  "Senior software engineer with 6+ years…". `validate_summary` rejects both corpus-wide unless
  the profile says them outright, and that check is load bearing rather than incidental: a
  seniority label nothing supports is the easiest fabrication on a resume and the one a reader
  is most likely to test. The prompt now names those exact phrases rather than leaving the model
  to infer that the rule covers them.
- **Exact keyword mirroring from the posting.** The same collision as the third revision.
- **"Mention AI-assisted development as a core part of your workflow."** One 2026 source pushed
  this hard. It is unsourceable unless the corpus says it, and it dates a resume to the month it
  was written.

Sources read in full: Resume Worded's software-engineer summary examples. Read as search
summaries only: Novoresume, BeamJobs, Teal, Kickresume, Enhancv. Tech Interview Handbook's
resume guide — the most credible of them for this role — returned 403 and was not read; if
this is revised again, start there. Where sources disagreed, the tiebreak was whichever advice
survived contact with `validate_summary`.

You can now edit all of this from `/prompts` in the dashboard, which writes a revision into
the `prompts` table and takes effect on the next call. The file here stays the default and
the way back. Because revisions are keyed by the same sha `llm_calls.system_sha` records, the
comparison above is a query, not a memory:

```sql
SELECT system_sha, count(*), round(avg(output_tokens)) FROM llm_calls
 WHERE task = 'tailor' GROUP BY 1 ORDER BY min(called_at);
```

**`tailor_check.md`** is the half of validation that used to be regex. It sees each emitted
line beside its own source row and answers one question: does the source say this. It does
**not** see the job description, and that is load-bearing — a checker that knows what job is
being applied for can be talked into a claim by how well it fits, and this is meant to be a
comparison rather than a judgement. It runs on a different model from `tailor` for the same
reason: two calls to one model share its blind spots.

Its contract is `{"verdicts": [{"n": <int>, "ok": <bool>, "claim": ..., "why": ...}]}`, with an
entry for every pair. A missing verdict is treated as a failure, not a pass — a line nothing
looked at is a line nothing checked.

The instruction that matters most is the one telling it what is *not* a violation: a line that
says less than its source is editing, and ordinary English the source happens not to use is
not a claim. Without that paragraph a checker rejects every rewrite, which is the failure mode
the regex had. `make test-live` scores it against the adversarial fixtures; watch the
legitimate cases there, not the fabrications.

**`score.md`** must return `{"score": <int 0-100>, "reason": "<string>"}`. An unparseable
reply leaves the posting in `discovered` for the next run; a batch of them wastes a paid
batch. The calibration bands are the part worth tuning — a scorer that puts everything at
70 has ordered nothing.

**`narrative.md`** returns plain text, no JSON. The only hard rule is grounding: it may
not invent an employer, project, technology, or metric the corpus does not support.
Nothing validates this one mechanically — you review every answer before it is sent.
