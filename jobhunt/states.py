"""The state machine. docs/architecture.md is the spec; this module is the enforcement.

Every `applications.state` write goes through `create` or `transition`, and each
writes its `events` row in the same transaction. Nothing else may UPDATE the
column — if you find yourself wanting to, add a transition to the table below and
to docs/architecture.md together.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import config
from .db import transaction

DISCOVERED = "discovered"
SCORED = "scored"
FILTERED = "filtered"
JOB_APPROVED = "job_approved"
SKIPPED = "skipped"
PACKET_READY = "packet_ready"
EXPIRED = "expired"
APPLIED = "applied"
REJECTED = "rejected"
INTERVIEW = "interview"
OFFER = "offer"

# (from_state, to_state). `None` means row creation — there is no prior state.
#
# (None, APPLIED) is the one addition to the table in docs/architecture.md: manual
# entry backfills an application I already submitted by hand, which never passed
# through discovery. It seeds an honest single event rather than manufacturing a
# discovered -> scored -> job_approved -> packet_ready history that never happened.
TRANSITIONS: frozenset[tuple[str | None, str]] = frozenset(
    {
        (None, DISCOVERED),  # ingest
        (None, APPLIED),  # manual entry / Phase 0 backfill
        (DISCOVERED, FILTERED),
        (DISCOVERED, SCORED),
        (SCORED, SKIPPED),
        (SCORED, JOB_APPROVED),
        (JOB_APPROVED, EXPIRED),
        (JOB_APPROVED, PACKET_READY),
        (PACKET_READY, EXPIRED),
        (PACKET_READY, APPLIED),
        (APPLIED, REJECTED),
        (APPLIED, INTERVIEW),
        (INTERVIEW, OFFER),
        (INTERVIEW, REJECTED),
        # Deliberately absent: (APPLIED, OFFER). An offer passes through interview.
    }
)

TERMINAL = frozenset({FILTERED, SKIPPED, EXPIRED, REJECTED, OFFER})

# Order for the funnel display, not a constraint.
PIPELINE_ORDER = (
    DISCOVERED,
    SCORED,
    JOB_APPROVED,
    PACKET_READY,
    APPLIED,
    INTERVIEW,
    OFFER,
)

# Reaching one of these is the first time a human at the company responded.
RESPONSE_STATES = frozenset({REJECTED, INTERVIEW, OFFER})

EVENT_KINDS = frozenset({"state_change", "email_in", "note", "interview", "digest_sent"})


class InvalidTransition(Exception):
    """A (from_state, to_state) pair absent from TRANSITIONS."""

    def __init__(self, from_state: str | None, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal transition {from_state or '—'} -> {to_state}")


def allowed_from(state: str | None) -> list[str]:
    """Legal next states, for rendering buttons instead of guessing at them."""
    return sorted(to for frm, to in TRANSITIONS if frm == state)


def _check(from_state: str | None, to_state: str) -> None:
    if (from_state, to_state) not in TRANSITIONS:
        raise InvalidTransition(from_state, to_state)


def create(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    state: str,
    detail: str | None = None,
    applied_at: str | None = None,
    would_apply_anyway: int | None = None,
    referral_contact_id: int | None = None,
    score: float | None = None,
    score_reasoning: str | None = None,
) -> int:
    """Insert an application in its seed state plus the matching event.

    `applications.job_id` is UNIQUE, so a duplicate raises IntegrityError — that
    is the guard against double-tracking one posting, not an `if exists` check.
    """
    _check(None, state)
    now = config.utcnow()
    if state == APPLIED and applied_at is None:
        applied_at = now

    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO applications (
              job_id, state, score, score_reasoning, referral_contact_id,
              would_apply_anyway, applied_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                state,
                score,
                score_reasoning,
                referral_contact_id,
                would_apply_anyway,
                applied_at,
                now,
                now,
            ),
        )
        application_id = int(cursor.lastrowid)
        _insert_event(conn, application_id, None, state, detail, occurred_at=now)
    return application_id


def transition(
    conn: sqlite3.Connection,
    application_id: int,
    to_state: str,
    *,
    detail: str | None = None,
    email_msg_id: str | None = None,
    occurred_at: str | None = None,
) -> str:
    """Move one application to `to_state`, writing its event in the same transaction.

    Sets `applied_at` on reaching `applied` and `first_response_at` on the first
    company response, so no caller has to remember to. Returns the prior state.
    """
    now = occurred_at or config.utcnow()
    with transaction(conn):
        row = conn.execute(
            "SELECT state, applied_at, first_response_at FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"no application {application_id}")

        from_state = row["state"]
        _check(from_state, to_state)

        columns = ["state = ?", "updated_at = ?"]
        values: list[Any] = [to_state, now]
        if to_state == APPLIED and row["applied_at"] is None:
            columns.append("applied_at = ?")
            values.append(now)
        if to_state in RESPONSE_STATES and row["first_response_at"] is None:
            columns.append("first_response_at = ?")
            values.append(now)
        if to_state in TERMINAL:
            columns.append("outcome = ?")
            values.append(to_state)
        values.append(application_id)

        conn.execute(
            f"UPDATE applications SET {', '.join(columns)} WHERE id = ?", values
        )
        _insert_event(
            conn,
            application_id,
            from_state,
            to_state,
            detail,
            email_msg_id=email_msg_id,
            occurred_at=now,
        )
    return from_state


def log_event(
    conn: sqlite3.Connection,
    application_id: int,
    kind: str,
    *,
    detail: str | None = None,
    email_msg_id: str | None = None,
) -> None:
    """Record a non-state event: a note, an inbound email, a digest send."""
    if kind == "state_change":
        raise ValueError("state changes go through transition(), not log_event()")
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO events (application_id, kind, detail, email_msg_id, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (application_id, kind, detail, email_msg_id, config.utcnow()),
        )


def _insert_event(
    conn: sqlite3.Connection,
    application_id: int,
    from_state: str | None,
    to_state: str,
    detail: str | None,
    *,
    email_msg_id: str | None = None,
    occurred_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO events (
          application_id, kind, from_state, to_state, detail, email_msg_id, occurred_at
        ) VALUES (?, 'state_change', ?, ?, ?, ?, ?)
        """,
        (application_id, from_state, to_state, detail, email_msg_id, occurred_at),
    )
