"""Answering the free-text boxes on one application form.

`answers.py` is the bank: twenty questions decided once, resolved per company,
never improvised. This is the other half — the boxes a particular form invents
that no catalogue anticipated, pasted in a batch and answered against the same
corpus.

The two are not parallel paths to the same thing, and the difference is the
invariant. A pasted question that is *really* a catalogued one — "are you legally
authorized to work in the United States?" is `work_authorized_us` however it is
worded — must be answered from the profile verbatim, never drafted. So the model
routes and the code substitutes: the reply carries a catalogue key or five
options, never both, and where a key comes back the recorded wording is copied in
without a model having touched it.

Five options rather than one because picking beats editing. A single draft gets
rewritten by hand every time; five different arguments usually contain one worth
sending, and choosing is faster than writing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import answers, config, llm, queries, tailor

# A form with more boxes than this is not being pasted in one go — the reply
# would be five answers times thirty questions and would truncate.
MAX_QUESTIONS = 12

MAX_PASTE_CHARS = 8000

OPTION_COUNT = 5

# Leading list markers real forms and real people paste: "1.", "2)", "-", "*",
# "Q3:", "•".
_MARKER = re.compile(r"^\s*(?:[-*•]|\(?\d+[.)]|Q\s*\d+[:.)]?)\s*", re.I)


class FormError(RuntimeError):
    """The paste could not be turned into questions. Written to be shown."""


@dataclass
class Drafted:
    question: str
    # "fact" when the profile already answers it, "draft" when it was written.
    source: str = "draft"
    key: str | None = None
    options: list[str] = field(default_factory=list)
    chosen: int | None = None
    # Figures in an option that no corpus row contains, per option index.
    unsourced: dict[str, list[str]] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "source": self.source,
            "key": self.key,
            "options": self.options,
            "chosen": self.chosen,
            "unsourced": self.unsourced,
            "note": self.note,
        }


def split_questions(pasted: str) -> list[str]:
    """A pasted blob into separate questions.

    Deterministic, and the parse is shown back on the page rather than trusted
    silently — a form that numbers its questions and one that separates them with
    blank lines both exist, and guessing wrong should be visible and editable
    rather than quietly answering the wrong thing.

    Blank lines win when present, because a multi-line question is common and
    splitting it per newline would turn one question into three.
    """
    text = pasted.strip()
    if not text:
        raise FormError("paste the questions first.")
    if len(text) > MAX_PASTE_CHARS:
        raise FormError(f"that is longer than {MAX_PASTE_CHARS} characters.")

    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) == 1:
        blocks = [line.strip() for line in text.splitlines() if line.strip()]

    out: list[str] = []
    for block in blocks:
        cleaned = _MARKER.sub("", block).strip()
        if cleaned:
            out.append(cleaned)
    if not out:
        raise FormError("no questions found in that.")
    if len(out) > MAX_QUESTIONS:
        raise FormError(
            f"that is {len(out)} questions; {MAX_QUESTIONS} at a time is the limit. "
            f"Split the form into two pastes."
        )
    return out


def _catalogue() -> str:
    """The settled questions, as keys the model can route to.

    Only the fact tier. A narrative question in the catalogue would be routed to
    a cached answer written for a different posting, and "why this company"
    reused across companies is the specific failure the cache exists to avoid.
    """
    lines = []
    for q in answers.QUESTIONS:
        if q.tier == answers.FACT:
            lines.append(f"- {q.key}: {q.text}")
    return "\n".join(lines)


def build_prompt(
    conn: sqlite3.Connection, application_id: int, questions: list[str]
) -> tuple[str, str]:
    """`(corpus, prompt)`, split so the corpus half caches."""
    row = queries.packet_row(conn, application_id)
    if row is None:
        raise LookupError("no such application")
    jd = (row["jd_text"] or "").strip()
    # The only caller that wants stories: "tell us about a time" has no answer in
    # a bullet, and this is the prompt that gets asked it.
    corpus, _ = tailor.build_prompt(
        conn, jd or "(no job description)", limit=None, include_stories=True
    )

    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    return corpus, (
        f"THE POSTING\n\n{jd or '(none stored)'}\n\n"
        f"QUESTIONS ALREADY SETTLED — route to these, never answer them\n\n"
        f"{_catalogue()}\n\n"
        f"THE FORM'S QUESTIONS\n\n{numbered}\n\n"
        f"Return the JSON object described in your instructions, with one entry "
        f"per question above, in order."
    )


def parse(conn: sqlite3.Connection, raw: str, asked: list[str]) -> list[Drafted]:
    """The reply into rows, with the fact substitution done here, not there."""
    text = raw.strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise FormError(f"no JSON object in the reply: {raw[:200]!r}")
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormError(f"the reply is not valid JSON: {exc}") from exc

    items = payload.get("answers")
    if not isinstance(items, list):
        raise FormError("the reply has no `answers` array")

    out: list[Drafted] = []
    for index, question in enumerate(asked):
        item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        key = item.get("key") or None
        options = [
            str(o).strip() for o in (item.get("options") or []) if str(o).strip()
        ]

        if key and key in answers.BY_KEY and answers.BY_KEY[key].tier == answers.FACT:
            # Routed to a settled question. Whatever the model may have written
            # is discarded unread: the recorded wording is the answer, verbatim,
            # and this is the branch where that invariant is actually enforced.
            resolved = _fact_answer(conn, key)
            out.append(
                Drafted(
                    question=question,
                    source="fact",
                    key=key,
                    options=[resolved] if resolved else [],
                    chosen=0 if resolved else None,
                    note=(
                        f"From your profile, verbatim ({key})."
                        if resolved
                        else f"This is a settled question ({key}) but your profile "
                        f"has no answer recorded for it yet."
                    ),
                )
            )
            continue

        drafted = Drafted(question=question, source="draft", options=options[:OPTION_COUNT])
        drafted.unsourced = {
            str(i): bad
            for i, option in enumerate(drafted.options)
            if (bad := _unsourced_numbers(conn, option))
        }
        out.append(drafted)
    return out


def _fact_answer(conn: sqlite3.Connection, key: str) -> str:
    """The recorded answer for a settled question, or "" if none is stored."""
    row = conn.execute(
        "SELECT answer FROM answers WHERE question_key = ? AND company_id IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    return str(row["answer"]).strip() if row and row["answer"] else ""


def _unsourced_numbers(conn: sqlite3.Connection, text: str) -> list[str]:
    """Figures in a drafted answer that no corpus row states.

    Same check the gap answers get, for the same reason: this goes into a box a
    person reads, and an invented number there is a fabrication with someone on
    the other end of it.
    """
    hay = " ".join(
        f"{b['text']} {b['metric'] or ''}" for b in queries.corpus_bullets(conn)
    )
    allowed = set(tailor._numbers(hay)) | tailor._spelled_numbers(hay)
    written = set(tailor._numbers(text, standalone_only=True)) | tailor._spelled_numbers(text)
    return sorted(n for n in written if n not in allowed)


def stored(conn: sqlite3.Connection, application_id: int) -> list[Drafted]:
    row = conn.execute(
        "SELECT form_answers FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None or not row["form_answers"]:
        return []
    try:
        parsed = json.loads(row["form_answers"])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        Drafted(
            question=str(d.get("question", "")),
            source=str(d.get("source", "draft")),
            key=d.get("key"),
            options=[str(o) for o in (d.get("options") or [])],
            chosen=d.get("chosen"),
            unsourced={k: list(v) for k, v in (d.get("unsourced") or {}).items()},
            note=str(d.get("note", "")),
        )
        for d in parsed
        if isinstance(d, dict)
    ]


def _save(conn: sqlite3.Connection, application_id: int, rows: list[Drafted]) -> None:
    conn.execute(
        "UPDATE applications SET form_answers = ?, updated_at = ? WHERE id = ?",
        (json.dumps([d.as_dict() for d in rows]), config.utcnow(), application_id),
    )


def draft(
    conn: sqlite3.Connection, application_id: int, pasted: str
) -> list[Drafted]:
    """Answer every box on the form in one call. Replaces any previous set."""
    questions = split_questions(pasted)
    corpus, prompt = build_prompt(conn, application_id, questions)
    raw = llm.complete(
        "form_answers",
        prompt,
        conn=conn,
        cached=corpus,
        application_id=application_id,
        expect_repeat=True,
    )
    rows = parse(conn, raw, questions)
    _save(conn, application_id, rows)
    return rows


def choose(
    conn: sqlite3.Connection, application_id: int, index: int, option: int
) -> list[Drafted]:
    """Pick an option, and remember a narrative one against the company.

    The bank is the point of remembering: the next application to this employer
    should not get a differently worded answer to the same question, which is
    the inconsistency `answers.py` exists to prevent. Fact answers are already
    the bank's and are not written back.
    """
    rows = stored(conn, application_id)
    if not 0 <= index < len(rows):
        raise LookupError("no such question")
    row = rows[index]
    if not 0 <= option < len(row.options):
        raise LookupError("no such option")
    row.chosen = option

    if row.source == "draft":
        context = queries.answer_context(conn, application_id)
        if context is not None:
            answers.put(
                conn,
                _key_for(row.question),
                row.question,
                row.options[option],
                tier=answers.NARRATIVE,
                company_id=int(context["company_id"]),
                # "generated", not "user": a model wrote these words and picking
                # one does not make the picker their author. `source` is how the
                # bank distinguishes what was written from what was typed, and a
                # chosen draft recorded as typed would quietly corrupt that.
                source="generated",
            )
    _save(conn, application_id, rows)
    return rows


def _key_for(question: str) -> str:
    """A stable key for an ad-hoc question, so the same box reuses its answer.

    Derived from the wording rather than assigned, because there is no catalogue
    to assign from — two forms asking the same thing in the same words should
    land on the same row, and two asking it differently are, for this purpose,
    two questions.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", question.lower()).strip("_")
    return f"form_{slug[:60]}" if slug else "form_question"
