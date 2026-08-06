"""Starting a worker from a button, without holding the request open.

Neither worker fits in a request. A sweep is 36 searches paced at a jittered
8-12 seconds; a scoring batch is polled for up to 45 minutes. So the click
claims the lock synchronously — which is what makes a double-click a clean 409
rather than two sweeps — and a daemon thread does the work while the page reads
`runs` for progress.

A thread rather than a task queue because there is exactly one of these at a
time, enforced by the database, and a queue would be a second thing to install,
supervise, and explain. Daemon, so shutdown is never held hostage to a batch
that has 40 minutes left; the row it abandons is closed out by
`runs.interrupt_owned` on the way down, or by the staleness rule if the process
was killed outright.

Preflight is the other half of the design: anything checkable in a millisecond —
a missing searches.yaml, an unreadable scoring.yaml, no API key — is checked
before the lock is taken, so it comes back as a red form message on the click
instead of a run that fails silently three minutes later.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Any

from .. import db, ingest, prefilter, runs, score, tailor

# The logger both `make dev` and the launchd agent are configured to show. A
# `jobhunt.*` logger is silently dropped under dev — see the note in app.py.
log = logging.getLogger("uvicorn.error")


class NotReady(RuntimeError):
    """Something a run needs is missing. The message is written to be shown."""


def _preflight(tasks: tuple[str, ...]) -> None:
    """Everything checkable before spending a lock, a request, or a cent."""
    try:
        if "ingest" in tasks:
            ingest.load_config()
        if "score" in tasks:
            prefilter.load()
        if {"score", "packet"} & set(tasks):
            if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
                raise NotReady("ANTHROPIC_API_KEY is not set — see .env.")
    except (ingest.IngestError, prefilter.PrefilterError) as exc:
        raise NotReady(str(exc)) from exc


def start(conn: sqlite3.Connection, pipeline: str) -> dict[str, Any]:
    """Claim step one and hand the chain to a thread. Returns the run.

    Raises `NotReady` for a bad config and `runs.AlreadyRunning` when something
    already holds the lock — including the 06:30 launchd agent, which is the
    whole reason the lock is in the database rather than in this process.
    """
    tasks = runs.PIPELINES.get(pipeline)
    if tasks is None:
        raise NotReady(
            f"{pipeline!r} is not something I can run. "
            f"Pick one of: {', '.join(sorted(runs.PIPELINES))}."
        )
    _preflight(tasks)

    # Claimed on the request's connection, in the request's thread, so the answer
    # to "is one already going?" is decided before the response is written.
    run_id = runs.claim(conn, tasks[0], chain=tasks, trigger="dashboard")
    threading.Thread(
        target=_work, args=(tasks, run_id), name=f"jobhunt-{pipeline}", daemon=True
    ).start()
    return runs.as_dict(runs.get(conn, run_id)) or {}


def start_packet(conn: sqlite3.Connection, application_id: int) -> dict[str, Any]:
    """Build one packet in the background, so the page can watch it.

    Separate from `start` because that runs a *pipeline* over everything in a
    state, and this answers a click on one application. It takes the same
    `packet` lock, which is what stops this and `build_pending` rendering the
    same row twice — and what makes a double-click a 409 rather than two builds
    and two bills.

    Held open rather than run inline for one reason: a build is two model calls,
    and a request that lasts that long cannot report what it is doing. The 202
    goes back immediately and the page reads `runs` for the phase.
    """
    _preflight(("packet",))
    run_id = runs.claim(conn, "packet", chain=("packet",), trigger="dashboard")
    threading.Thread(
        target=_work_one,
        args=(application_id, run_id),
        name=f"jobhunt-packet-{application_id}",
        daemon=True,
    ).start()
    return runs.as_dict(runs.get(conn, run_id)) or {}


def _work_one(application_id: int, run_id: int) -> None:
    """One packet, on its own connection. See `_work` for why."""
    try:
        conn = db.connect()
    except Exception as exc:  # noqa: BLE001 - the disk can be gone by now
        log.error("jobhunt: could not open the database for packet: %s", exc)
        return
    try:
        with runs.attached(conn, run_id) as report:
            tailor.build_packet(conn, application_id, on_progress=report)
    except Exception as exc:  # noqa: BLE001 - a thread that raises dies silently
        # `runs.attached` has already recorded this on the row, which is where
        # the page reads it from. This line is for the log.
        log.warning(
            "jobhunt: packet %s failed — %s: %s", application_id, type(exc).__name__, exc
        )
    finally:
        conn.close()


def _work(tasks: tuple[str, ...], first_run_id: int) -> None:
    """The background thread.

    Opens its own connection: a sqlite3 connection belongs to the thread that
    created it, and the request's is closed the moment the 202 is written.
    """
    try:
        conn = db.connect()
    except Exception as exc:  # noqa: BLE001 - the disk can be gone by now
        log.error("jobhunt: could not open the database for %s: %s", tasks[0], exc)
        return
    try:
        run_id = first_run_id
        for index, task in enumerate(tasks):
            if index:
                # Step one has finished and released its lock; step two claims
                # its own. Someone else may have taken it in between — the 06:30
                # agent's score, most likely — and that is not a failure, it is
                # the same work being done by someone else.
                try:
                    run_id = runs.claim(conn, task, chain=tasks, trigger="dashboard")
                except runs.AlreadyRunning as exc:
                    log.warning("jobhunt: %s not started — %s", task, exc)
                    return
            if not _step(conn, task, run_id):
                # A failed step stops the chain. Scoring on top of a sweep that
                # died halfway would report a number for a run that did not
                # happen, which is worse than stopping and saying so.
                return
    finally:
        conn.close()


def _step(conn: sqlite3.Connection, task: str, run_id: int) -> bool:
    """One task. `runs.attached` records the outcome either way."""
    try:
        with runs.attached(conn, run_id) as report:
            if task == "ingest":
                ingest.run(conn, on_progress=report)
            elif task == "score":
                score.run(conn, on_progress=report)
            else:
                tailor.build_pending(conn, on_progress=report)
    except Exception as exc:  # noqa: BLE001 - a thread that raises dies silently
        log.warning("jobhunt: %s failed — %s: %s", task, type(exc).__name__, exc)
        return False
    return True
