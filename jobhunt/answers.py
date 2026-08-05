"""The answer bank: what goes in the boxes a resume cannot fill.

Every ATS asks the same twenty questions in slightly different words. Three of
them need thought per company; the rest I decided once and should never
improvise again — inconsistent answers to knockout questions end applications.

Three tiers, and the distinction is an invariant rather than a convenience:

  fact, global      From `docs/profile/facts.yaml`. Returned **verbatim**.
                    Never LLM-generated, never paraphrased. CLAUDE.md is
                    explicit, and the reason is that "are you authorized to work
                    in the US" has exactly one correct answer and a model that
                    rephrases it has introduced a chance of getting it wrong.

  fact, per-company Typed by me, stored against a company, reused forever after.
                    Salary is the whole category: the number depends on the
                    job's market, so there is no global default — but once I
                    have quoted a figure to an employer, every later application
                    to them has to repeat it exactly.

  narrative         Generated once per company and cached. "Why this company" is
                    the only kind of question worth a model call, and caching
                    per company is what stops the second application to one
                    employer contradicting the first.

Resolution, per `docs/architecture.md`: company override → global → generate if
narrative → otherwise record it in `unknown_questions` and flag the packet.
Nothing is ever invented for a fact-tier question. A missing fact is surfaced as
missing, because a wrong answer there is worse than an empty box.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import config, llm
from .db import transaction

FACT = "fact"
NARRATIVE = "narrative"


class AnswerError(RuntimeError):
    """An answer could not be resolved or generated."""


@dataclass(frozen=True)
class Question:
    """One question a form asks, and where its answer comes from."""

    key: str
    text: str          # the wording to show me; real forms vary, this is canonical
    tier: str
    facts_path: str | None = None   # dotted key in facts.yaml, for global facts
    per_company: bool = False       # typed by me per employer, never generated
    # Blank in facts.yaml on purpose rather than not filled in yet. The file says
    # "omit if N/A" for exactly these, so counting them as unknowns would put
    # permanent noise in the one counter meant to show real gaps.
    optional: bool = False
    note: str = ""


# The catalogue. Ordered the way forms ask, so the packet reads top to bottom
# next to the actual application.
#
# Deliberately short. facts.yaml already argues the case: street address, EEO
# responses, "how did you hear about us" and the rest are things a password
# manager fills better, and nobody cross-references them. The test for adding
# one is whether I would have to *decide* or *look up* the answer.
QUESTIONS: tuple[Question, ...] = (
    Question("work_authorized_us",
             "Are you legally authorized to work in the United States?",
             FACT, "decided_once.work_authorized_us",
             note="Knockout. A wrong answer ends the application."),
    Question("requires_sponsorship",
             "Will you now or in the future require visa sponsorship?",
             FACT, "decided_once.requires_sponsorship",
             note="Knockout, and the one most often answered inconsistently."),
    Question("visa_note", "Visa status detail, if asked", FACT, "decided_once.visa_note",
             optional=True, note="Blank means not applicable."),
    Question("earliest_start", "When is the earliest you could start?",
             FACT, "decided_once.earliest_start"),
    Question("notice_period", "What notice do you have to give?",
             FACT, "decided_once.notice_period"),
    Question("onsite_tolerance", "Can you work onsite / hybrid as required?",
             FACT, "decided_once.onsite_tolerance"),
    Question("relocation", "Are you willing to relocate?",
             FACT, "decided_once.relocation"),
    Question("relocation_metros", "Which locations would you relocate to?",
             FACT, "decided_once.relocation_metros"),
    Question("clearance", "Do you hold an active security clearance?",
             FACT, "decided_once.clearance",
             optional=True, note="Blank means no clearance; answer 'no' on the form."),
    # Per-company fact. No global default on purpose — see the module docstring.
    Question("salary_expectation", "What are your salary expectations?",
             FACT, None, per_company=True,
             note="Typed by me per employer and then reused verbatim, so I never "
                  "quote one employer two different numbers."),
    # The only questions worth a model call.
    Question("why_this_company", "Why do you want to work at this company?", NARRATIVE),
    Question("why_this_role", "Why are you interested in this role?", NARRATIVE),
)

BY_KEY = {question.key: question for question in QUESTIONS}


# =================================== storage ===================================


def put(
    conn: sqlite3.Connection,
    key: str,
    text: str,
    answer: str,
    *,
    tier: str,
    source: str,
    company_id: int | None = None,
) -> None:
    """Upsert one answer. The unique indexes decide global vs per-company.

    001 has `UNIQUE (question_key, company_id)` and 003 added the partial index
    for the `company_id IS NULL` case, which SQLite would otherwise treat as
    never conflicting. Both are needed; the conflict targets below match them.
    """
    conflict = "(question_key)" if company_id is None else "(question_key, company_id)"
    where = "WHERE company_id IS NULL" if company_id is None else ""
    with transaction(conn):
        conn.execute(
            f"""
            INSERT INTO answers
                (question_key, question_text, tier, company_id, answer, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT {conflict} {where} DO UPDATE SET
              answer = excluded.answer,
              question_text = excluded.question_text,
              tier = excluded.tier,
              source = excluded.source
            """,
            (key, text, tier, company_id, answer, source, config.utcnow()),
        )


def _lookup(
    conn: sqlite3.Connection, key: str, company_id: int | None
) -> sqlite3.Row | None:
    if company_id is not None:
        row = conn.execute(
            "SELECT * FROM answers WHERE question_key = ? AND company_id = ?",
            (key, company_id),
        ).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM answers WHERE question_key = ? AND company_id IS NULL", (key,)
    ).fetchone()


def record_unknown(
    conn: sqlite3.Connection, question_text: str, application_id: int | None = None
) -> None:
    """A question with no answer. Counted, not guessed at.

    `seen_count` is the useful part: a question asked once is noise, one asked
    six times is the next thing to add to facts.yaml. That is what `make chat`
    walks.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT id, seen_count FROM unknown_questions WHERE question_text = ?",
            (question_text,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO unknown_questions (question_text, application_id, created_at) "
                "VALUES (?, ?, ?)",
                (question_text, application_id, config.utcnow()),
            )
        else:
            conn.execute(
                "UPDATE unknown_questions SET seen_count = seen_count + 1 WHERE id = ?",
                (int(row["id"]),),
            )


# ================================== generating ==================================


def generate(
    conn: sqlite3.Connection,
    question: Question,
    *,
    company: str,
    title: str,
    jd_text: str | None,
    company_id: int,
    application_id: int | None = None,
) -> str:
    """Draft a narrative answer and cache it against the company.

    Cached per company rather than per application on purpose: two applications
    to one employer that give different reasons for wanting to work there is
    the failure this whole table exists to prevent.
    """
    if question.tier != NARRATIVE:
        # The invariant, enforced rather than documented. A fact reaching a
        # model is the bug this guards, and it would be invisible in the output.
        raise AnswerError(
            f"{question.key!r} is {question.tier}-tier and must never be generated. "
            f"Fact answers are returned verbatim."
        )

    from . import score  # profile summary, built from the corpus

    jd = (jd_text or "").strip()
    prompt = "\n".join([
        f"QUESTION: {question.text}",
        "",
        f"Company: {company}",
        f"Role: {title}",
        "",
        "POSTING",
        jd[:4000] if jd else "(no description available)",
    ])
    text = llm.complete(
        "narrative",
        prompt,
        conn=conn,
        cached=score._profile_prefix(conn),
        application_id=application_id,
        # One company at a time from the packet button; nothing follows inside
        # the cache TTL, so writing an entry would be a 125% charge never read.
        expect_repeat=False,
    ).strip()
    if not text:
        raise AnswerError(f"the model returned nothing for {question.key!r}")
    put(conn, question.key, question.text, text, tier=NARRATIVE,
        source="generated", company_id=company_id)
    return text


# ================================== resolving ==================================


@dataclass
class Resolved:
    question: Question
    answer: str | None
    source: str | None          # user | generated | None
    scope: str                  # company | global | missing
    generatable: bool = False   # narrative with nothing cached yet

    @property
    def missing(self) -> bool:
        return self.answer is None


def resolve_all(
    conn: sqlite3.Connection, company_id: int | None
) -> list[Resolved]:
    """The whole answer set for one application. Reads only — never generates.

    Generation costs money and takes seconds, so it is a button on the packet
    rather than something a page render triggers. Anything narrative and
    uncached comes back `generatable`.
    """
    out = []
    for question in QUESTIONS:
        row = _lookup(conn, question.key, company_id)
        if row is None:
            out.append(Resolved(question, None, None, "missing",
                                generatable=question.tier == NARRATIVE))
            continue
        out.append(Resolved(
            question=question,
            answer=row["answer"],
            source=row["source"],
            scope="company" if row["company_id"] is not None else "global",
        ))
    return out


def flag_unknowns(
    conn: sqlite3.Connection, resolved: list[Resolved], application_id: int | None
) -> int:
    """Record every unanswered fact question. Returns how many.

    Narrative gaps are not unknowns — they are a button I have not pressed yet,
    and neither are the facts facts.yaml deliberately leaves blank. A missing
    *required* fact is different: it means the file has a hole, and the point of
    counting is that the sixth time I hit the same empty box is when it
    obviously belongs in there.
    """
    count = 0
    for item in resolved:
        if (item.missing and item.question.tier == FACT
                and not item.question.per_company and not item.question.optional):
            record_unknown(conn, item.question.text, application_id)
            count += 1
    return count
