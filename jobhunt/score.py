"""Pass 2 of scoring: the LLM judges fit, and only orders the digest.

`prefilter` has already run and rejected what would genuinely be refused, so
everything still in `discovered` is something worth a look. This assigns each a
0-100 fit score and one sentence of reasoning, then moves it to `scored`.

**A score never advances state.** CLAUDE.md is explicit: it orders the digest and
nothing else. Nothing here approves, skips, or filters — a 3/100 posting reaches
`scored` exactly like a 95, it just sorts last. If a low score ever starts
causing a rejection, the deterministic rules in `prefilter` are the place to put
it, where the reason is inspectable.

Batch by default, per `config/models.yaml`. Scoring runs before the 8am digest
and is not latency-sensitive, so the Batch API's 50% discount is free money: the
whole 3,510-posting backlog costs about $3 instead of $6. Batches are submitted
and polled; nothing is held in memory between the two, so a killed run resumes
by re-reading `discovered`.

Cost is bounded by dedup, not by this file. A posting is scored once ever —
`apply_url_norm` means a rerun of ingest inserts nothing, and a scored row has
left `discovered`. Steady state is only what is genuinely new since last run.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Any

from . import config, llm, prefilter, states
from .db import connect, transaction

# The scorer reads enough to judge fit, not the whole posting. Past roughly this
# much, a JD is benefits, EEO boilerplate, and legal text — which costs tokens
# and tells the model nothing about whether I should apply. Measured across
# 3,510 real postings the median JD is well under this; the tail is the noise.
MAX_JD_CHARS = 6000

# A batch that would take longer than this to come back is not going to make the
# 8am digest, and waiting is not the worker's job — the rows stay `discovered`
# and the next run picks them up.
BATCH_POLL_SECONDS = 15
BATCH_MAX_WAIT = 45 * 60


class ScoreError(RuntimeError):
    """Scoring could not run, or the model's reply was unusable."""


_SYSTEM = """You score how well one job posting fits one candidate. \
You are ordering a shortlist, not making a decision — the candidate reviews \
every posting you pass and submits every application by hand.

Return ONLY a JSON object, no prose:
{"score": <integer 0-100>, "reason": "<one sentence, under 25 words>"}

Score on fit to the candidate's actual background: the technologies they have \
shipped, the kind of work they have done, and the level implied by the posting.

Do NOT score on compensation — the candidate does not filter on it.
Do NOT score on location — that is ranked separately and never rejects.
Do NOT penalise a posting for a short or vague description; judge what is there.

Calibration, because a scorer that puts everything at 70 has ordered nothing:
  85-100  squarely this candidate's work — the stack and the level both match
  60-84   plausible: adjacent stack, or right stack at a slightly off level
  30-59   a stretch — same field, little overlap in what they have built
   0-29   wrong discipline, or a level far from theirs"""


@dataclass
class Scored:
    application_id: int
    score: float
    reason: str


def _profile_prefix(conn: sqlite3.Connection) -> str:
    """The candidate summary every scoring call shares, byte for byte.

    Identical across all ~600 calls a month, which is exactly the shape prompt
    caching wants — CLAUDE.md ranks it the first cost lever, ahead of the Batch
    API and model tier. Built from the corpus rather than hand-written so it
    cannot drift from what the resume claims.
    """
    from . import queries

    facts = queries.profile_facts(conn)
    lines = ["CANDIDATE", ""]
    if facts.get("identity.headline"):
        lines.append(facts["identity.headline"])

    for row in queries.corpus_experiences(conn):
        span = f"{row['start_month']}–{row['end_month'] or 'present'}"
        lines.append(f"- {row['title']} ({span})")
        if row["scope"]:
            lines.append(f"    {row['scope']}")
    for row in queries.corpus_projects(conn):
        lines.append(f"- project: {row['name']} — {row['role'] or ''}".rstrip(" —"))
        if row["blurb"]:
            lines.append(f"    {str(row['blurb'])[:220]}")

    tech: list[str] = []
    for source in (queries.corpus_experiences(conn), queries.corpus_projects(conn)):
        for row in source:
            for column in ("tech_built", "tech_owned", "tech_maintained", "tech_touched"):
                if column in row.keys() and row[column]:
                    try:
                        tech.extend(str(item) for item in json.loads(row[column]))
                    except json.JSONDecodeError:
                        pass
    if tech:
        seen = list(dict.fromkeys(tech))
        lines += ["", "TECHNOLOGIES", ", ".join(seen)]

    for row in queries.corpus_education(conn):
        lines.append(f"\nEDUCATION\n{row['degree'] or ''} {row['field'] or ''}".rstrip())

    prefix = "\n".join(lines)
    # Refuse rather than score against nothing. An unloaded corpus produces the
    # word CANDIDATE and two newlines, which the model will happily score
    # against — returning plausible numbers derived from no information at all,
    # for every posting, with no error anywhere to notice.
    if len(prefix) < 200:
        raise ScoreError(
            "the profile corpus is empty or nearly so, so every score would be "
            "made up. Run `make load-profile` first."
        )
    return prefix


def _posting_prompt(row: sqlite3.Row) -> str:
    jd = (row["jd_text"] or "").strip()
    if len(jd) > MAX_JD_CHARS:
        jd = jd[:MAX_JD_CHARS] + "\n[truncated]"
    parts = [
        "JOB POSTING",
        "",
        f"Title: {row['title']}",
        f"Company: {row['company']}",
    ]
    if row["location"]:
        parts.append(f"Location: {row['location']} ({row['remote'] or 'unknown'})")
    if jd:
        parts += ["", jd]
    else:
        parts += ["", "(no description available — score on the title alone)"]
    parts += ["", "Return the JSON object described in your instructions."]
    return "\n".join(parts)


def parse_score(raw: str) -> tuple[float, str]:
    """Pull `{score, reason}` out of a reply. Raises if it is not there.

    Deliberately strict about the number and forgiving about everything else: a
    fenced block or a sentence of preamble is a formatting quirk, but a score
    that is not a number in range would silently reorder the digest.
    """
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ScoreError(f"no JSON object in the reply: {raw[:160]!r}")
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScoreError(f"reply is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "score" not in payload:
        raise ScoreError(f"reply has no `score`: {text[:160]!r}")
    try:
        score = float(payload["score"])
    except (TypeError, ValueError) as exc:
        raise ScoreError(f"score is not a number: {payload['score']!r}") from exc
    if not 0 <= score <= 100:
        raise ScoreError(f"score {score} is outside 0-100")
    return score, str(payload.get("reason", "")).strip()[:500]


def pending(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Everything the prefilter left in `discovered`, oldest first."""
    sql = """
        SELECT a.id AS application_id, j.title, j.jd_text, j.location, j.remote,
               c.name AS company
          FROM applications a
          JOIN jobs j      ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.state = ?
         ORDER BY a.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (states.DISCOVERED,)).fetchall()


def apply_score(conn: sqlite3.Connection, result: Scored) -> None:
    """One posting: record the score and move it to `scored`, together."""
    with transaction(conn):
        conn.execute(
            "UPDATE applications SET score = ?, score_reasoning = ?, updated_at = ? "
            "WHERE id = ?",
            (result.score, result.reason, config.utcnow(), result.application_id),
        )
        states.transition(
            conn,
            result.application_id,
            states.SCORED,
            detail=f"score {result.score:.0f}: {result.reason}",
        )


# =================================== batch ===================================


def score_batch(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], *, wait: bool = True
) -> dict[str, int]:
    """Submit one Batch API job for `rows`, then poll and apply the results."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise ScoreError(
            "the anthropic package is not installed. Install it with: "
            "uv pip install -e '.[llm]'"
        ) from exc

    settings = llm.task_config("score")
    model = settings["model"]
    llm.rate(model)  # refuse before spending, not after logging $0
    prefix = _profile_prefix(conn)

    system: list[dict[str, Any]] = [{"type": "text", "text": prefix}]
    if settings.get("cache_profile"):
        # Byte-identical across every request in the batch and across every run
        # this month. This is the lever CLAUDE.md ranks first.
        system[0]["cache_control"] = {"type": "ephemeral"}
    system.append({"type": "text", "text": _SYSTEM})

    # Kept so results can be logged with the prompt that produced them.
    # `llm_calls.prompt` is NOT NULL, and CLAUDE.md asks for the prompt on every
    # call — the first live batch died here passing None.
    prompts = {f"app-{int(row['application_id'])}": _posting_prompt(row) for row in rows}
    requests = [
        {
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": int(settings.get("max_tokens", 200)),
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
        }
        for custom_id, prompt in prompts.items()
    ]

    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests)
    # Printed before anything can go wrong with it. A batch is already paid for
    # once submitted, and results stay retrievable for 29 days — so this id is
    # what turns a crash or a timeout into `--batch-id <id>` instead of paying
    # a second time. The first live run needed exactly this.
    print(f"  submitted batch {batch.id} — {len(requests)} postings", file=sys.stderr)
    if not wait:
        print(f"  --no-wait: rerun with --batch-id {batch.id} to apply the results.",
              file=sys.stderr)
        return {"submitted": len(requests), "scored": 0, "failed": 0}

    deadline = time.monotonic() + BATCH_MAX_WAIT
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if time.monotonic() > deadline:
            # Rows stay `discovered`, so the next run re-submits them. Not ideal
            # — it pays twice — but it never loses a posting, and a batch this
            # slow has already missed the digest it was for.
            raise ScoreError(
                f"batch {batch.id} still running after {BATCH_MAX_WAIT // 60} minutes. "
                f"It is already paid for and results keep for 29 days — apply them "
                f"later with:  make score ARGS='--batch-id {batch.id}'"
            )
        time.sleep(BATCH_POLL_SECONDS)

    return apply_batch(conn, batch.id, model=model, prompts=prompts,
                       submitted=len(requests))


def apply_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    model: str | None = None,
    prompts: dict[str, str] | None = None,
    submitted: int | None = None,
) -> dict[str, int]:
    """Apply the results of an already-submitted batch.

    Split out so a crash, a timeout, or `--no-wait` does not waste a batch that
    has already been paid for. Idempotent by way of the state machine: a row
    already moved out of `discovered` raises InvalidTransition and is counted as
    skipped rather than scored twice.
    """
    import anthropic

    client = anthropic.Anthropic()
    prompts = prompts or {}
    model = model or llm.task_config("score")["model"]

    counts = {"submitted": submitted or 0, "scored": 0, "failed": 0, "already": 0}
    for entry in client.messages.batches.results(batch_id):
        application_id = int(str(entry.custom_id).removeprefix("app-"))
        if entry.result.type != "succeeded":
            counts["failed"] += 1
            continue
        message = entry.result.message
        text = "".join(part.text for part in message.content if part.type == "text")
        try:
            score, reason = parse_score(text)
        except ScoreError:
            # One unparseable reply is not a reason to lose the run. The row
            # stays `discovered` and gets another go next time.
            counts["failed"] += 1
            continue
        try:
            apply_score(conn, Scored(application_id, score, reason))
        except states.InvalidTransition:
            # Already scored — a re-applied batch, or the row moved on. Not an
            # error, and re-scoring it would overwrite a decision I may have made.
            counts["already"] += 1
            continue
        counts["scored"] += 1
        _log_batch_call(conn, model, application_id, message,
                        prompts.get(str(entry.custom_id), ""))
    return counts


def _log_batch_call(
    conn: sqlite3.Connection, model: str, application_id: int, message: Any,
    prompt: str = "",
) -> None:
    """`llm_calls` row per batch result. CLAUDE.md wants every call logged.

    Cost is halved: the Batch API bills at 50%, and reconstructing it from the
    token counts is the only way `llm_calls.cost_usd` stays true — the API
    still does not return a price.
    """
    usage = getattr(message, "usage", None)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO llm_calls (task, model, application_id, prompt, response,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                cost_usd, latency_ms, error, stop_reason, called_at)
            VALUES ('score', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                model,
                application_id,
                # NOT NULL. Empty string when a batch is re-applied from its id
                # and the prompts are no longer in memory — honest about what is
                # known rather than a crash or a fabricated reconstruction.
                prompt,
                "".join(p.text for p in message.content if p.type == "text")[:2000],
                (getattr(usage, "input_tokens", 0) or 0)
                + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                if usage
                else None,
                getattr(usage, "output_tokens", None) if usage else None,
                getattr(usage, "cache_read_input_tokens", None) if usage else None,
                getattr(usage, "cache_creation_input_tokens", None) if usage else None,
                llm._cost(model, usage) * 0.5 if usage else None,
                getattr(message, "stop_reason", None),
                config.utcnow(),
            ),
        )


# ================================ synchronous ================================


def score_one(conn: sqlite3.Connection, row: sqlite3.Row) -> Scored:
    """One posting, one call. For `--sync`, which exists to check the prompt.

    Batch is right for a queue and wrong for iterating on wording — a 24-hour
    turnaround per attempt makes the prompt impossible to tune.
    """
    prefix = _profile_prefix(conn)
    raw = llm.complete(
        "score",
        _posting_prompt(row),
        conn=conn,
        system=_SYSTEM,
        cached=prefix,
        application_id=int(row["application_id"]),
    )
    score, reason = parse_score(raw)
    return Scored(int(row["application_id"]), score, reason)


# =================================== driving ===================================


def run(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    use_batch: bool = True,
    wait: bool = True,
    run_prefilter: bool = True,
) -> dict[str, int]:
    """Prefilter, then score whatever survived."""
    counts: dict[str, int] = {}
    if run_prefilter:
        counts.update({f"prefilter_{k}": v for k, v in prefilter.run(conn).items()})

    rows = pending(conn, limit)
    if not rows:
        counts["scored"] = 0
        return counts

    if use_batch:
        counts.update(score_batch(conn, rows, wait=wait))
        return counts

    scored = failed = 0
    for row in rows:
        try:
            apply_score(conn, score_one(conn, row))
            scored += 1
        except (ScoreError, llm.LLMError) as exc:
            print(f"  ! application {row['application_id']}: {exc}", file=sys.stderr)
            failed += 1
    counts.update({"scored": scored, "failed": failed})
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prefilter, then LLM-score `discovered`.")
    parser.add_argument("--limit", type=int, default=None, help="score at most N postings")
    parser.add_argument(
        "--sync", action="store_true",
        help="one call per posting instead of a batch — for checking the prompt, costs 2x",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="submit the batch and exit without polling for results",
    )
    parser.add_argument(
        "--batch-id", default=None,
        help="apply the results of an already-submitted batch instead of creating one",
    )
    parser.add_argument(
        "--prefilter-only", action="store_true",
        help="run pass 1 and stop — deterministic, free, and the usual thing to check first",
    )
    args = parser.parse_args(argv)

    conn = connect()
    try:
        if args.batch_id:
            counts = apply_batch(conn, args.batch_id)
        elif args.prefilter_only:
            counts = prefilter.run(conn, limit=args.limit)
        else:
            counts = run(
                conn, limit=args.limit, use_batch=not args.sync, wait=not args.no_wait
            )
    except (ScoreError, llm.LLMError, prefilter.PrefilterError) as exc:
        print(f"scoring failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
