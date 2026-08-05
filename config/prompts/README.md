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
tailor.md     builds one tailored resume       -> tailor.py
score.md      ranks one posting 0-100          -> score.py  (sync and batch)
narrative.md  drafts one application answer    -> answers.py
```

A task routed in `models.yaml` with no file here fails at call time with the path it
looked for. `classify_email` is deliberately absent until Phase 5 gives it a call site.

## What the code depends on

These prompts are not free text. Each one has a contract with a parser or a validator,
and breaking it fails the whole call rather than degrading quietly.

**`tailor.md`** is the tightly coupled one. `tailor.py` validates every returned bullet
against its source row and raises `FabricationError` on any violation — the model gets
exactly one retry, then the packet build fails. Instructions that exist to keep output
inside the validator, not to make it read better:

- the JSON envelope `{"reasoning": ..., "bullets": [{"id": ..., "text": ...}]}`
- *start every bullet with a verb* — a capitalized first word reads as a proper-noun claim
- *use the corpus's own vocabulary* — "an LLM" for a source that says Claude is unsourced
- *never change a number, or add one the source lacks*
- *never merge two bullets* — one output bullet per source row, or the id mapping breaks
- *keep `shared: true` work visibly shared*

Loosen any of those and rejections go up, not quality. If you want different output,
change what the prompt asks for *within* those rules — which bullets to favour, how long,
what to lead with.

**`score.md`** must return `{"score": <int 0-100>, "reason": "<string>"}`. An unparseable
reply leaves the posting in `discovered` for the next run; a batch of them wastes a paid
batch. The calibration bands are the part worth tuning — a scorer that puts everything at
70 has ordered nothing.

**`narrative.md`** returns plain text, no JSON. The only hard rule is grounding: it may
not invent an employer, project, technology, or metric the corpus does not support.
Nothing validates this one mechanically — you review every answer before it is sent.
