"""Editing a task's system prompt from the dashboard.

An overlay on `config/prompts/<task>.md`, not a replacement — see 012_prompts.sql
for why the file stays the default. Everything here is about the layer on top:
which wording is live, which ones have been live, and what each one produced.

The last part is the reason this exists rather than being a text box. A prompt
edit is only worth making if you can tell afterwards whether it helped, and
`llm_calls.system_sha` already records which wording produced which resume. This
module keys revisions by that same sha, so "the resumes got worse on Tuesday"
becomes a join rather than a memory.

This module owns its table's SQL, as `states.py` and `runs.py` own theirs.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import config, db, llm


class PromptError(ValueError):
    """The edit was rejected. The message is written to be shown."""


# A prompt shorter than this is a truncated save or a cleared box, not an
# instruction. An empty system block is accepted by the API and quietly produces
# markedly worse output, which is the kind of regression that gets blamed on the
# model — so it is refused here as well as in `llm.system_prompt`.
MIN_BODY_CHARS = 40


def tasks() -> list[str]:
    """Every task with a prompt, from models.yaml. The editor lists these."""
    return sorted(llm.routing().get("tasks") or {})


def file_body(task: str) -> str:
    """The git-tracked default, whatever the database currently says."""
    path = llm.prompt_path(task)
    return path.read_text().strip() if path.exists() else ""


def active(conn: sqlite3.Connection, task: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM prompts WHERE task = ? AND active = 1", (task,)
    ).fetchone()


def history(conn: sqlite3.Connection, task: str) -> list[dict[str, Any]]:
    """Every saved revision, newest first, with what it produced.

    The call count and spend come from `llm_calls` by sha, which is the whole
    point of keying revisions this way: a revision with 40 calls behind it is a
    judgement you can actually make, and one with none is a guess.
    """
    rows = conn.execute(
        """
        SELECT p.sha, p.created_at, p.active, p.note, length(p.body) AS size,
               (SELECT count(*) FROM llm_calls c
                 WHERE c.task = p.task AND c.system_sha = p.sha) AS calls,
               (SELECT round(sum(c.cost_usd), 4) FROM llm_calls c
                 WHERE c.task = p.task AND c.system_sha = p.sha) AS cost
          FROM prompts p
         WHERE p.task = ?
         ORDER BY p.created_at DESC, p.sha
        """,
        (task,),
    ).fetchall()
    return [
        {
            "sha": row["sha"],
            "created_at": row["created_at"],
            "active": bool(row["active"]),
            "note": row["note"] or "",
            "chars": int(row["size"]),
            "calls": int(row["calls"] or 0),
            "cost": float(row["cost"] or 0.0),
        }
        for row in rows
    ]


def view(conn: sqlite3.Connection, task: str) -> dict[str, Any]:
    """Everything the editor shows for one task."""
    if task not in tasks():
        raise PromptError(f"no task {task!r}. Known: {', '.join(tasks())}.")
    live = active(conn, task)
    default = file_body(task)
    body = str(live["body"]) if live else default
    settings = llm.task_config(task)
    path = llm.prompt_path(task)
    return {
        "task": task,
        "body": body,
        "sha": llm.prompt_sha(body) if body else "",
        # Which of the two is actually in force, so the editor never has to
        # guess and can offer Revert only when there is something to revert to.
        "source": "database" if live else "file",
        "file_body": default,
        "file_path": str(
            path.relative_to(config.REPO_ROOT)
            if path.is_relative_to(config.REPO_ROOT)
            else path
        ),
        "differs_from_file": body.strip() != default.strip(),
        "model": settings.get("model", ""),
        "history": history(conn, task),
    }


def save(
    conn: sqlite3.Connection, task: str, body: str, note: str = ""
) -> dict[str, Any]:
    """Store `body` as a revision and make it the live one.

    Editing back to a wording already saved re-activates that revision rather
    than storing a second copy — the primary key is `(task, sha)`, so the same
    text is the same revision by definition, and the call counts behind it stay
    attached to it.
    """
    if task not in tasks():
        raise PromptError(f"no task {task!r}. Known: {', '.join(tasks())}.")
    text = body.strip()
    if len(text) < MIN_BODY_CHARS:
        raise PromptError(
            f"that is {len(text)} characters. A system prompt shorter than "
            f"{MIN_BODY_CHARS} is a truncated save, and an empty one silently "
            f"makes every answer worse. Use Revert to go back to the file."
        )
    sha = llm.prompt_sha(text)
    with db.transaction(conn):
        # Cleared first: `idx_prompts_active` allows one active row per task, so
        # the insert below would collide rather than take over.
        conn.execute("UPDATE prompts SET active = 0 WHERE task = ?", (task,))
        conn.execute(
            """
            INSERT INTO prompts (task, sha, body, created_at, active, note)
                 VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT (task, sha) DO UPDATE
                    SET active = 1, note = COALESCE(NULLIF(excluded.note, ''), note)
            """,
            (task, sha, text, config.utcnow(), note.strip() or None),
        )
    return view(conn, task)


def activate(conn: sqlite3.Connection, task: str, sha: str) -> dict[str, Any]:
    """Make a previously saved revision live again."""
    row = conn.execute(
        "SELECT sha FROM prompts WHERE task = ? AND sha = ?", (task, sha)
    ).fetchone()
    if row is None:
        raise PromptError(f"no revision {sha!r} for {task}.")
    with db.transaction(conn):
        conn.execute("UPDATE prompts SET active = 0 WHERE task = ?", (task,))
        conn.execute(
            "UPDATE prompts SET active = 1 WHERE task = ? AND sha = ?", (task, sha)
        )
    return view(conn, task)


def revert(conn: sqlite3.Connection, task: str) -> dict[str, Any]:
    """Drop back to the file. The revisions stay; only the override is lifted.

    Always available, and deliberately non-destructive: the file is the version
    git can show you a diff of, and the way back from an edit that made things
    worse should not itself require an edit.
    """
    if task not in tasks():
        raise PromptError(f"no task {task!r}. Known: {', '.join(tasks())}.")
    conn.execute("UPDATE prompts SET active = 0 WHERE task = ?", (task,))
    return view(conn, task)


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """One line per task, for the index."""
    out = []
    for task in tasks():
        live = active(conn, task)
        default = file_body(task)
        body = str(live["body"]) if live else default
        out.append({
            "task": task,
            "model": llm.task_config(task).get("model", ""),
            "source": "database" if live else "file",
            "sha": llm.prompt_sha(body) if body else "",
            "chars": len(body),
            "updated_at": str(live["created_at"]) if live else None,
            "missing": not body,
        })
    return {"prompts": out}
