"""Talking to the model about one built resume.

The packet page's other buttons are one-shot: build, rebuild, download. This is
the one place the resume can be argued with — "drop the Docker Compose claim",
"lead with the retrieval work", "why did you leave out the migration bullet" —
and it exists because the checks stopped blocking. A finding is now a note beside
a line rather than a refusal, so there has to be somewhere to act on it that is
faster than rebuilding and hoping for different wording.

It is a genuine continuation, not a briefing. `llm_calls` kept both halves of the
call that built the resume, so those become turn one and the model is reading its
own prompt, its own output, and the reasoning it wrote at the time. Asked why a
bullet was dropped, it answers from the decision rather than reconstructing one.
That is also why there is no `tailor_revise.md`: the system prompt is still
`tailor.md`, because the rules did not change and swapping the system prompt
underneath a conversation is not the same session in any sense that matters.

Two shapes of turn, and the difference is load bearing:

  a question     -> a reply, and nothing changes. "Why that bullet" deserves a
                    sentence, not a new resume. The model returns the bullets it
                    was given; `parse_reply` compares them to what is stored and
                    reports no proposal.
  an instruction -> a reply plus a *proposal*: the whole resume as it would then
                    read. Nothing is written to `applications` until the proposal
                    is applied, so a revision that made things worse costs a
                    click rather than the draft.

Applying one runs the identical validate -> render -> store path a build does,
via `tailor.apply_selection`, including recomputing findings. There is
deliberately no second way to write a resume into the database.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import config, llm, queries, tailor

# Enough turns to argue a resume into shape, few enough that the prompt stays
# under the corpus in size. Older turns fall off the top; the current resume is
# always sent in full, so nothing that matters is only in the scrollback.
HISTORY_TURNS = 12

MAX_MESSAGE_CHARS = 4000


class ChatError(RuntimeError):
    """The turn could not be completed. The message is written to be shown."""


def messages(conn: sqlite3.Connection, application_id: int) -> list[dict[str, Any]]:
    """The whole thread, oldest first."""
    rows = conn.execute(
        "SELECT id, role, content, proposal, applied_at, created_at FROM packet_chat "
        "WHERE application_id = ? ORDER BY id",
        (application_id,),
    ).fetchall()
    out = []
    for row in rows:
        proposal = None
        if row["proposal"]:
            try:
                proposal = json.loads(row["proposal"])
            except json.JSONDecodeError:
                proposal = None
        out.append({
            "id": int(row["id"]),
            "role": row["role"],
            "content": row["content"],
            # The client needs to know a proposal exists and how big the change
            # is, not the whole document — it renders the diff from `bullets`.
            "proposal": proposal,
            "applied_at": row["applied_at"],
            "created_at": row["created_at"],
        })
    return out


def _append(
    conn: sqlite3.Connection,
    application_id: int,
    role: str,
    content: str,
    proposal: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO packet_chat (application_id, role, content, proposal, created_at) "
        "VALUES (?,?,?,?,?)",
        (
            application_id,
            role,
            content,
            json.dumps(proposal) if proposal else None,
            config.utcnow(),
        ),
    )
    return int(cur.lastrowid or 0)


def _emitted_index(conn: sqlite3.Connection, application_id: int) -> dict[str, int]:
    """`final text -> source bullet id`, from the call that wrote the resume.

    Matching a stored line back to its source row by text cannot work: the whole
    job of the tailor is to reword, so the rendered line is *not* the corpus
    sentence. On a real packet this recovers 10 of 10 where text matching
    recovers none.

    The reply that produced those exact strings is in `llm_calls` with the ids
    still attached, so that is the index. Several are read because the newest
    call may have been a chat turn whose proposal was never applied.
    """
    index: dict[str, int] = {}
    for row in conn.execute(
        "SELECT response FROM llm_calls "
        " WHERE application_id = ? AND task IN ('tailor', 'tailor_revise') "
        "   AND error IS NULL AND response IS NOT NULL AND response != '' "
        " ORDER BY id DESC LIMIT 6",
        (application_id,),
    ):
        try:
            emitted, _, _ = tailor.parse_response(row["response"])
        except tailor.TailorError:
            continue
        for b in emitted:
            if isinstance(b, dict) and "id" in b and str(b.get("text") or "").strip():
                # First writer wins: rows come newest-first, and the newest reply
                # that used a given wording is the one that put it on the page.
                index.setdefault(str(b["text"]).strip(), int(b["id"]))
    return index


# The sections whose `highlights` are `bullets` rows. Education carries its
# `notes` in the same field, and an education note is not a resume line: it has
# no bullet id, so it can never be proposed or validated, and including it made
# `parse_reply` compare a stored list the model could not possibly return —
# which reads as a change on every turn and offers an Apply button for one.
_BULLET_SECTIONS = ("experience", "projects")


def current_resume(conn: sqlite3.Connection, application_id: int) -> list[dict[str, Any]]:
    """The resume as stored, as (id, text) pairs.

    Read back from `resume_data` — the document that produced the stored bytes —
    rather than re-deriving it, for the same reason `packet_view` does: anything
    else describes a resume that was never downloaded.
    """
    row = conn.execute(
        "SELECT resume_data FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None or not row["resume_data"]:
        return []
    try:
        document = json.loads(row["resume_data"])
    except json.JSONDecodeError:
        return []
    sections = document.get("cv", {}).get("sections", {}) or {}
    texts: list[str] = []
    for name in _BULLET_SECTIONS:
        for entry in sections.get(name) or ():
            if isinstance(entry, dict):
                texts.extend(entry.get("highlights", []) or [])
    emitted = _emitted_index(conn, application_id)
    # Verbatim lines still resolve straight off the corpus, and that path needs
    # no model call to have been logged — it is the fallback when a packet
    # predates the log or its rows were pruned.
    sources = {b["text"]: int(b["id"]) for b in queries.corpus_bullets(conn)}
    out: list[dict[str, Any]] = []
    for text in texts:
        key = text.strip()
        bullet_id = emitted.get(key) or sources.get(key) or sources.get(text)
        out.append({"id": bullet_id, "text": text})
    return out


def numbered(lines: list[dict[str, Any]]) -> str:
    """The resume as the model is shown it.

    One function so the numbering on the page and the numbering in the prompt
    cannot drift. "Line 3 is weak" has to mean the same line on both sides, and
    that only holds while one piece of code decides what line 3 is.
    """
    return "\n".join(
        f"{i}. [source bullet {b['id'] if b['id'] is not None else '?'}] {b['text']}"
        for i, b in enumerate(lines, start=1)
    )


def resume_text(conn: sqlite3.Connection, application_id: int) -> dict[str, Any] | None:
    """The stored resume as plain text, for reading and quoting.

    None when nothing is built. The PDF is the artifact that gets submitted, but
    it is a bad thing to read on a phone and impossible to quote from into a
    chat box, which is the actual workflow this serves.
    """
    lines = current_resume(conn, application_id)
    if not lines:
        return None
    summary = _stored_summary(conn, application_id)
    body = numbered(lines)
    return {
        "summary": summary,
        "lines": [
            {"n": i, "id": b["id"], "text": b["text"]}
            for i, b in enumerate(lines, start=1)
        ],
        # Ready to paste somewhere else entirely — a cover letter, an ATS box.
        # Without the source-bullet annotations, which are for the model.
        "plain": (f"{summary}\n\n" if summary else "")
        + "\n".join(f"• {b['text']}" for b in lines),
        "prompt_view": body,
    }


def _stored_summary(conn: sqlite3.Connection, application_id: int) -> str:
    row = conn.execute(
        "SELECT resume_data FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None or not row["resume_data"]:
        return ""
    try:
        document = json.loads(row["resume_data"])
    except json.JSONDecodeError:
        return ""
    sections = document.get("cv", {}).get("sections", {}) or {}
    for name, entries in sections.items():
        if "summary" in str(name).lower():
            for entry in entries:
                if isinstance(entry, str):
                    return entry
                if isinstance(entry, dict) and entry.get("text"):
                    return str(entry["text"])
    return ""


def _findings_text(conn: sqlite3.Connection, application_id: int) -> str:
    row = conn.execute(
        "SELECT resume_findings FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None or not row["resume_findings"]:
        return ""
    try:
        found = json.loads(row["resume_findings"])
    except json.JSONDecodeError:
        return ""
    if not isinstance(found, list) or not found:
        return ""
    lines = []
    for f in found:
        where = f.get("where", "?")
        message = f.get("message", "")
        source = f.get("source") or ""
        lines.append(f"- {where}: {message}" + (f"\n  source: {source}" if source else ""))
    return "THE CHECKS OBJECTED TO:\n" + "\n".join(lines)


def build_exchange(conn: sqlite3.Connection, application_id: int) -> list[dict[str, str]]:
    """The build's own exchange, replayed as turns.

    This is the difference between continuing the session that wrote the resume
    and describing it to a model that never saw it. The tailor call logged both
    halves to `llm_calls`, so the real prompt and the real reply — including the
    `reasoning` field saying what it optimized for — become turn one. Asked
    afterwards why a bullet was dropped, the model is reading its own decision
    rather than guessing at one.

    Empty when the build predates this table or was never made; `send` falls back
    to describing the resume, which still works and is simply worse.
    """
    row = conn.execute(
        "SELECT prompt, response FROM llm_calls "
        " WHERE application_id = ? AND task = 'tailor' AND error IS NULL "
        "   AND response IS NOT NULL AND response != '' "
        " ORDER BY id DESC LIMIT 1",
        (application_id,),
    ).fetchone()
    if row is None:
        return []
    return [
        {"role": "user", "content": row["prompt"]},
        {"role": "assistant", "content": row["response"]},
    ]


def build_turn(
    conn: sqlite3.Connection, application_id: int, instruction: str
) -> tuple[str, list[dict[str, str]], str]:
    """`(corpus, history, prompt)`.

    `corpus` is the cacheable system block, exactly as `tailor` sends it, so the
    cached prefix is byte-identical to the build's and to every prior turn's.
    """
    row = queries.packet_row(conn, application_id)
    if row is None:
        raise LookupError("no such application")
    jd = (row["jd_text"] or "").strip()
    corpus, _ = tailor.build_prompt(conn, jd or "(no job description)", limit=None)

    lines = current_resume(conn, application_id)
    if not lines:
        raise ChatError("there is no resume to talk about yet — build the packet first.")

    history = build_exchange(conn, application_id)
    for turn in messages(conn, application_id)[-HISTORY_TURNS:]:
        history.append({"role": turn["role"], "content": turn["content"]})

    parts: list[str] = []
    if not history:
        # No logged build to replay — describe the resume instead. Strictly worse
        # and only reachable for packets built before this existed, but a chat
        # that refuses to open is worse still.
        parts.append(
            f"THE POSTING:\n{jd or '(none stored)'}\n\n"
            f"THE RESUME AS IT STANDS:\n"
            f"Summary: {_stored_summary(conn, application_id) or '(none)'}\n\n"
            f"{numbered(lines)}"
        )
    else:
        # The resume may have been revised and re-rendered since the exchange
        # above, so state what is actually stored now rather than letting the
        # model assume its last proposal is what is on the page.
        parts.append(
            f"THE RESUME CURRENTLY STORED (this is what the PDF contains):\n"
            f"Summary: {_stored_summary(conn, application_id) or '(none)'}\n\n"
            f"{numbered(lines)}"
        )

    objections = _findings_text(conn, application_id)
    if objections:
        parts.append(objections)
    parts.append(instruction.strip())
    return corpus, history, "\n\n".join(parts)


def parse_reply(
    conn: sqlite3.Connection, application_id: int, raw: str
) -> tuple[str, dict[str, Any] | None]:
    """`(reply, proposal or None)`, from the ordinary tailor envelope.

    There is no revision-specific envelope, because there is no
    revision-specific prompt: the model is continuing the conversation that
    produced the resume and still answering under `tailor.md`, so it replies in
    the shape that prompt asks for — `{reasoning, summary, bullets}`. The
    `reasoning` field, which on a build explains what was optimized for, is the
    conversational reply here.

    A turn that changes nothing — answering "why did you drop the Redis bullet"
    — comes back with the same bullets it was given. That is compared against
    what is stored and reported as no proposal, so the page does not offer to
    apply a resume identical to the one already on it.

    Falls back to the raw text when the envelope is missing entirely. The model
    answering a question in prose is a usable answer, and losing a turn the user
    paid for over a formatting slip is worse than a reply with no Apply button.
    """
    try:
        emitted, reasoning, summary = tailor.parse_response(raw)
    except tailor.TailorError:
        return raw.strip(), None

    reply = reasoning.strip()
    cleaned = [
        {"id": b["id"], "text": str(b["text"]).strip()}
        for b in emitted
        if isinstance(b, dict) and "id" in b and str(b.get("text") or "").strip()
    ]
    if not cleaned:
        return reply or raw.strip(), None

    stored = current_resume(conn, application_id)
    unchanged = [
        {"id": b["id"], "text": b["text"]} for b in stored
    ] == cleaned and (_stored_summary(conn, application_id).strip() == summary.strip())
    if unchanged:
        return reply or raw.strip(), None
    return reply, {"summary": summary.strip(), "bullets": cleaned}


def send(
    conn: sqlite3.Connection,
    application_id: int,
    instruction: str,
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    """One turn. Records both sides and returns the assistant's message."""
    said = instruction.strip()
    if not said:
        raise ChatError("say something first.")
    if len(said) > MAX_MESSAGE_CHARS:
        raise ChatError(f"that is longer than {MAX_MESSAGE_CHARS} characters.")

    report = on_progress or (lambda **_: None)
    # Built before the turn is recorded, so `history` is the conversation as it
    # stood when they spoke — appending first would send their message twice.
    corpus, history, prompt = build_turn(conn, application_id, said)
    _append(conn, application_id, "user", said)

    report(phase="selecting", message="thinking about the change")
    raw = llm.complete(
        "tailor_revise",
        prompt,
        conn=conn,
        cached=corpus,
        application_id=application_id,
        expect_repeat=True,
        history=history,
    )
    reply, proposal = parse_reply(conn, application_id, raw)
    message_id = _append(conn, application_id, "assistant", reply, proposal)
    return {
        "id": message_id,
        "role": "assistant",
        "content": reply,
        "proposal": proposal,
        "applied_at": None,
    }


def apply(
    conn: sqlite3.Connection, application_id: int, message_id: int, *, on_progress: Any = None
) -> None:
    """Render and store a proposal, through the same path a build uses.

    Not a second way to write a resume: this hands the proposal's selection to
    `tailor` machinery, so the checks run, findings are recomputed, and the
    overwrite is logged exactly as a rebuild is.
    """
    row = conn.execute(
        "SELECT proposal, applied_at FROM packet_chat WHERE id = ? AND application_id = ?",
        (message_id, application_id),
    ).fetchone()
    if row is None:
        raise LookupError("no such message")
    if not row["proposal"]:
        raise ChatError("that message did not propose a change.")
    proposal = json.loads(row["proposal"])
    tailor.apply_selection(
        conn,
        application_id,
        emitted=proposal["bullets"],
        summary=proposal.get("summary", ""),
        on_progress=on_progress,
        detail="revised in chat",
    )
    conn.execute(
        "UPDATE packet_chat SET applied_at = ? WHERE id = ?",
        (config.utcnow(), message_id),
    )
