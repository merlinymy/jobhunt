"""One ingest and one score at a time, and a live account of what they are doing.

The lock lives in `runs`, not in this process, because the callers are separate
processes: the launchd agent at 06:30 and 18:30, `make ingest` in a terminal, and
the dashboard's background thread. See 010_runs.sql for why that has to be a
database constraint rather than a check in Python.

This module owns its table's SQL the way `states.py` owns `events` — the locking
rules and the statements that enforce them are one thing, and splitting them
across `queries.py` would leave the interesting half without the other.

Progress is a snapshot overwritten in place, not a history. Workers take an
optional `on_progress` callback and stay unaware of any of this: passing None —
which is what the CLI does when it only wants the lock, and what the test
fixtures do — restores exactly the old behaviour.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from . import config, db

TASKS = ("ingest", "score")

# What the dashboard button can ask for. `ingest_score` is the one that is
# actually useful: ingest alone leaves rows in `discovered`, which never reach
# the review queue, so the pair is the smallest thing that changes what I see.
PIPELINES: dict[str, tuple[str, ...]] = {
    "ingest": ("ingest",),
    "score": ("score",),
    "ingest_score": ("ingest", "score"),
}

# How long a `running` row may go without reporting before anyone may take its
# lock. Every loop in both workers reports at least every ~15 seconds — the
# scoring batch poll is the slowest — so ten minutes of silence means genuinely
# wedged, not merely busy.
STALE_AFTER_SECONDS = 10 * 60

# Progress writes are throttled to this. A phase change always writes.
MIN_WRITE_SECONDS = 1.0

# What each worker phase is called on screen. Here rather than in TypeScript for
# the same reason the state vocabulary is served from /api/meta: the phases
# belong to the workers, and adding one should not need a second edit in another
# language to stop the dashboard printing a bare identifier at me.
PHASE_LABELS = {
    "starting": "Starting",
    "searching": "Searching Indeed",
    "boards": "Polling company boards",
    "storing": "Storing postings",
    "prefilter": "Applying the filter rules",
    "submitting": "Preparing the scoring batch",
    "submitted": "Batch submitted",
    "waiting": "Waiting on the scoring batch",
    "applying": "Recording scores",
    "scoring": "Scoring",
    "finished": "Finishing up",
}

# Long enough for the batch-recovery sentence, which is the one error here worth
# reading in full; short enough that a runaway repr cannot bloat the row.
MAX_ERROR_CHARS = 4000

HOST = socket.gethostname()

_ARTICLE = {"ingest": "An", "score": "A"}


class AlreadyRunning(RuntimeError):
    """Someone else holds this task's lock. The message names who."""

    def __init__(self, message: str, run: sqlite3.Row | None = None) -> None:
        super().__init__(message)
        self.run = run


class ProgressFn(Protocol):
    """What a worker is handed. Every field is optional — a call updates the
    fields it names and leaves the rest of the snapshot alone."""

    def __call__(
        self,
        *,
        phase: str | None = None,
        message: str | None = None,
        done: int | None = None,
        total: int | None = None,
        counts: Mapping[str, int] | None = None,
    ) -> None: ...


@dataclass
class Progress:
    """The snapshot the dashboard renders. `total` is None when the denominator
    is genuinely unknown, which is the difference between a progress bar and a
    spinner — do not invent one."""

    phase: str = "starting"
    message: str = ""
    done: int = 0
    total: int | None = None
    counts: dict[str, int] = field(default_factory=dict)


# ==================================== time ====================================


def _age_seconds(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def ago(ts: str | None) -> str:
    """"3m", "6h" — how long since a timestamp, for a status line."""
    return short_duration(_age_seconds(ts))


def short_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 90 * 60:
        return f"{int(seconds / 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


# ==================================== lock ====================================


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return True  # unknown, and the heartbeat rule is the backstop
    return True


def _dead_reason(row: sqlite3.Row) -> str | None:
    """Why this `running` row's owner is provably gone, or None to leave it be.

    Two rules, and it matters that the age one is not vetoed by a live pid.
    After a reboot macOS hands out low pids again, so the pid recorded before
    the crash is quite likely to belong to some unrelated process now — and a
    lock nobody holds, that nothing can ever reclaim, is a worse failure than
    the one the veto would prevent. A worker that has said nothing for ten
    minutes is wedged on a socket, not hammering anyone.

    The pid check is only meaningful on the host that wrote the row. It exists
    for the common case — `make dev` reloading mid-sweep — where waiting ten
    minutes to press the button again would be its own small insult.
    """
    if row["host"] == HOST and not _pid_alive(int(row["pid"])):
        return "the process that started it is gone"
    age = _age_seconds(row["heartbeat_at"])
    if age is not None and age > STALE_AFTER_SECONDS:
        return f"nothing was reported for {short_duration(age)}"
    return None


def describe(row: sqlite3.Row) -> str:
    """The sentence a blocked caller is shown. Names the trigger, because
    "already running" is a different problem depending on who started it."""
    age = _age_seconds(row["started_at"])
    return (
        f"{_ARTICLE.get(row['task'], 'A')} {row['task']} is already running "
        f"({row['trigger']}, pid {row['pid']}, started {row['started_at']}, "
        f"{short_duration(age)} ago)."
    )


def reclaim_stale(conn: sqlite3.Connection, task: str | None = None) -> int:
    """Mark abandoned `running` rows `interrupted`. Returns how many."""
    sql = "SELECT * FROM runs WHERE state = 'running'"
    rows = (
        conn.execute(sql + " AND task = ?", (task,)).fetchall()
        if task
        else conn.execute(sql).fetchall()
    )
    reclaimed = 0
    for row in rows:
        reason = _dead_reason(row)
        if reason is None:
            continue
        finish(
            conn,
            int(row["id"]),
            state="interrupted",
            error=f"Abandoned — {reason}.",
        )
        reclaimed += 1
    return reclaimed


def _require_table(conn: sqlite3.Connection) -> None:
    """A pulled-but-unmigrated checkout otherwise fails with `no such table:
    runs` from somewhere inside a worker, which reads like a bug rather than a
    missing step. One cheap query, twice a day plus button presses."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone():
        raise RuntimeError(
            "the `runs` table is missing, so the one-at-a-time guard cannot be "
            "taken and two sweeps could run at once. Run `make migrate`."
        )


def claim(
    conn: sqlite3.Connection,
    task: str,
    *,
    chain: Sequence[str] = (),
    trigger: str = "cli",
) -> int:
    """Take the lock for `task`, or raise `AlreadyRunning`. Returns the run id."""
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}")
    _require_table(conn)
    reclaim_stale(conn, task)

    now = config.utcnow()
    opening = Progress(phase="starting", message=f"Starting {task}…")
    try:
        with db.transaction(conn):
            cursor = conn.execute(
                """
                INSERT INTO runs (task, chain, state, trigger, host, pid,
                                  started_at, heartbeat_at, progress)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task,
                    ",".join(chain or (task,)),
                    trigger,
                    HOST,
                    os.getpid(),
                    now,
                    now,
                    json.dumps(asdict(opening)),
                ),
            )
    except sqlite3.IntegrityError as exc:
        # `idx_runs_one_active`. Not an error in the code — it is the guard, and
        # the only honest thing to do is name the run that got there first.
        live = active(conn, task)
        raise AlreadyRunning(
            describe(live) if live else f"{task} is already running.", live
        ) from exc
    return int(cursor.lastrowid or 0)


def finish(
    conn: sqlite3.Connection, run_id: int, *, state: str, error: str | None = None
) -> None:
    """Close a run out. Only ever moves a row out of `running`, never back into
    it — a worker that finishes after being reclaimed does not get to overwrite
    the reclaim, because by then something else may hold the lock."""
    now = config.utcnow()
    conn.execute(
        "UPDATE runs SET state = ?, finished_at = ?, heartbeat_at = ?, error = ? "
        "WHERE id = ? AND state = 'running'",
        (state, now, now, (error or None) and error[:MAX_ERROR_CHARS], run_id),
    )


def interrupt_owned(conn: sqlite3.Connection) -> int:
    """Close out this process's own running rows. Called on dashboard shutdown,
    so a restart does not leave a phantom run for the heartbeat to time out."""
    rows = conn.execute(
        "SELECT id FROM runs WHERE state = 'running' AND host = ? AND pid = ?",
        (HOST, os.getpid()),
    ).fetchall()
    for row in rows:
        finish(
            conn,
            int(row["id"]),
            state="interrupted",
            error="The dashboard stopped while this was running.",
        )
    return len(rows)


# ================================== progress ==================================


class Reporter:
    """Writes one run's progress, and keeps its heartbeat fresh.

    Call it from the thread that owns `conn`, and never from inside a
    transaction: a progress snapshot has nothing to be atomic with, and joining
    the caller's transaction would let a rollback erase the last thing the page
    was told.
    """

    def __init__(self, conn: sqlite3.Connection, run_id: int) -> None:
        self.conn = conn
        self.run_id = run_id
        self.progress = Progress()
        self._last_write = 0.0

    def __call__(
        self,
        *,
        phase: str | None = None,
        message: str | None = None,
        done: int | None = None,
        total: int | None = None,
        counts: Mapping[str, int] | None = None,
    ) -> None:
        moved_on = phase is not None and phase != self.progress.phase
        if phase is not None:
            self.progress.phase = phase
        if message is not None:
            self.progress.message = message
        if done is not None:
            self.progress.done = done
        if total is not None:
            self.progress.total = total
        if counts is not None:
            self.progress.counts = dict(counts)
        # A phase change is always worth a write — it is the line the page
        # actually reads — and it also resets `total`, so a throttled write here
        # would leave a bar sitting at 34/36 while the next phase runs.
        if moved_on or time.monotonic() - self._last_write >= MIN_WRITE_SECONDS:
            self.flush()

    def flush(self) -> None:
        self.conn.execute(
            "UPDATE runs SET progress = ?, heartbeat_at = ? WHERE id = ?",
            (json.dumps(asdict(self.progress)), config.utcnow(), self.run_id),
        )
        self._last_write = time.monotonic()


def _settle(
    conn: sqlite3.Connection,
    reporter: Reporter,
    run_id: int,
    state: str,
    error: str | None = None,
) -> None:
    """Record an outcome, best effort.

    If the volume went away mid-run neither write can succeed, and raising here
    would replace the real exception with a second one from the same dead disk.
    The row stays `running` and `reclaim_stale` collects it once the heartbeat
    ages out, which is the correct fallback rather than a silent one.
    """
    for write in (reporter.flush, lambda: finish(conn, run_id, state=state, error=error)):
        try:
            write()
        except Exception:  # noqa: BLE001 - see the docstring
            pass


@contextmanager
def attached(conn: sqlite3.Connection, run_id: int) -> Iterator[Reporter]:
    """Report progress for an already-claimed run, and close it out on the way
    out. Split from `track` so the dashboard can claim in the request — where a
    double-click gets a synchronous 409 — and run in a thread."""
    reporter = Reporter(conn, run_id)
    try:
        yield reporter
    except KeyboardInterrupt:
        _settle(conn, reporter, run_id, "interrupted", "Stopped by hand (Ctrl-C).")
        raise
    except BaseException as exc:
        _settle(conn, reporter, run_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    _settle(conn, reporter, run_id, "done")


@contextmanager
def track(
    conn: sqlite3.Connection,
    task: str,
    *,
    chain: Sequence[str] = (),
    trigger: str | None = None,
) -> Iterator[Reporter]:
    """Claim, report, close out. What the CLI entry points wrap themselves in."""
    run_id = claim(conn, task, chain=chain, trigger=trigger or default_trigger())
    with attached(conn, run_id) as reporter:
        yield reporter


def default_trigger() -> str:
    """`launchd` when the scheduled wrapper set it, else `cli`.

    deploy/discover.sh exports it. Worth the one line: "already running" reads
    very differently depending on whether the 06:30 agent or your own forgotten
    terminal is holding the lock.
    """
    trigger = (os.environ.get("JOBHUNT_TRIGGER") or "cli").strip()
    return trigger if trigger in ("dashboard", "cli", "launchd") else "cli"


# =================================== reading ==================================


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """The live run and the last of each task, read as one statement.

    One statement deliberately, so it is one snapshot. Three separate SELECTs
    can land between a step finishing and the next one claiming, and the page
    then sees a chain with nothing running and nothing finished — which is the
    exact moment the client is watching for to announce a result.
    """
    rows = conn.execute(
        """
        SELECT * FROM runs
         WHERE state = 'running'
            OR id IN (SELECT max(id) FROM runs GROUP BY task)
         ORDER BY id DESC
        """
    ).fetchall()
    last: dict[str, Any] = {task: None for task in TASKS}
    for row in rows:  # id DESC, so the first of each task is its newest
        if last.get(row["task"]) is None:
            last[row["task"]] = as_dict(row)
    running = next((row for row in rows if row["state"] == "running"), None)
    return {"active": as_dict(running), "last": last}


def active(conn: sqlite3.Connection, task: str | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM runs WHERE state = 'running'"
    if task:
        return conn.execute(sql + " AND task = ?", (task,)).fetchone()
    return conn.execute(sql + " ORDER BY id DESC").fetchone()


def get(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def latest(conn: sqlite3.Connection, task: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE task = ? ORDER BY id DESC LIMIT 1", (task,)
    ).fetchone()


def as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """One run, shaped for the client. `step`/`steps` are derived here rather
    than in TypeScript so a page opened mid-chain knows it is on step 2 of 2."""
    if row is None:
        return None
    chain = [task for task in (row["chain"] or "").split(",") if task]
    try:
        progress = json.loads(row["progress"]) if row["progress"] else None
    except json.JSONDecodeError:
        progress = None
    return {
        "id": int(row["id"]),
        "task": row["task"],
        "chain": chain,
        "step": chain.index(row["task"]) + 1 if row["task"] in chain else 1,
        "steps": len(chain) or 1,
        "state": row["state"],
        "trigger": row["trigger"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "age_seconds": _age_seconds(row["finished_at"] or row["started_at"]),
        "progress": progress,
        "error": row["error"],
    }
