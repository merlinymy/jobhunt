"""Thin query module. Parameterized SQL in named functions. No ORM.

State writes are not here — they live in states.py, which is the only place
`applications.state` is written.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import config, states
from .db import transaction
from .normalize import detect_ats, norm_company_name, norm_title, normalize_apply_url

# ============================== companies / jobs ==============================


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    *,
    careers_url: str | None = None,
    ats_type: str | None = None,
    ats_slug: str | None = None,
) -> int:
    """Find or create by `name_norm`. ATS fields fill in only when still empty."""
    name_norm = norm_company_name(name)
    if not name_norm:
        raise ValueError(f"company name normalizes to empty: {name!r}")

    with transaction(conn):
        row = conn.execute(
            "SELECT id FROM companies WHERE name_norm = ?", (name_norm,)
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                """
                INSERT INTO companies (name, name_norm, ats_type, ats_slug, careers_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name.strip(), name_norm, ats_type, ats_slug, careers_url),
            )
            return int(cursor.lastrowid)

        company_id = int(row["id"])
        conn.execute(
            """
            UPDATE companies
               SET ats_type    = COALESCE(ats_type, ?),
                   ats_slug    = COALESCE(ats_slug, ?),
                   careers_url = COALESCE(careers_url, ?)
             WHERE id = ?
            """,
            (ats_type, ats_slug, careers_url, company_id),
        )
        return company_id


def find_job_by_url(conn: sqlite3.Connection, apply_url: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE apply_url_norm = ?", (normalize_apply_url(apply_url),)
    ).fetchone()


def find_similar_jobs(
    conn: sqlite3.Connection, company_id: int, title: str, location: str | None
) -> list[sqlite3.Row]:
    """Soft dedup: same company, same normalized title, same location."""
    return conn.execute(
        """
        SELECT j.*, a.id AS application_id, a.state
          FROM jobs j
          LEFT JOIN applications a ON a.job_id = j.id
         WHERE j.company_id = ? AND j.title_norm = ?
               AND IFNULL(j.location, '') = IFNULL(?, '')
        """,
        (company_id, norm_title(title), location),
    ).fetchall()


def insert_job(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    title: str,
    apply_url: str,
    source: str,
    location: str | None = None,
    remote: str | None = None,
    jd_text: str | None = None,
    comp_min: int | None = None,
    comp_max: int | None = None,
    posted_at: str | None = None,
) -> int:
    """Insert a job. `apply_url_norm` is UNIQUE — a duplicate raises IntegrityError."""
    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO jobs (
              company_id, title, title_norm, location, remote, apply_url, apply_url_norm,
              jd_text, comp_min, comp_max, source, posted_at, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id,
                title.strip(),
                norm_title(title),
                location,
                remote or "unknown",
                apply_url.strip(),
                normalize_apply_url(apply_url),
                jd_text,
                comp_min,
                comp_max,
                source,
                posted_at,
                config.utcnow(),
            ),
        )
        return int(cursor.lastrowid)


# ================================ applications ================================

# Every list and detail view wants the same join. One string, one shape.
_APPLICATION_SELECT = """
SELECT a.*,
       j.title, j.location, j.remote, j.apply_url, j.apply_url_norm, j.source,
       j.comp_min, j.comp_max, j.posted_at, j.discovered_at, j.jd_text,
       c.id AS company_id, c.name AS company_name,
       ct.name AS referral_name, ct.channel AS referral_channel,
       ct.handle AS referral_handle
  FROM applications a
  JOIN jobs j      ON j.id = a.job_id
  JOIN companies c ON c.id = j.company_id
  LEFT JOIN contacts ct ON ct.id = a.referral_contact_id
"""


def list_applications(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    active_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if state:
        where.append("a.state = ?")
        params.append(state)
    if active_only:
        placeholders = ", ".join("?" * len(states.TERMINAL))
        where.append(f"a.state NOT IN ({placeholders})")
        params.extend(sorted(states.TERMINAL))

    sql = _APPLICATION_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(a.applied_at, a.updated_at) DESC, a.id DESC LIMIT ?"
    params.append(limit)
    return [_decorate(row) for row in conn.execute(sql, params)]


def get_application(conn: sqlite3.Connection, application_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        _APPLICATION_SELECT + " WHERE a.id = ?", (application_id,)
    ).fetchone()
    return _decorate(row) if row else None


def _decorate(row: sqlite3.Row) -> dict[str, Any]:
    """Add read-derived fields. ATS is regexed from the URL, never stored."""
    record = dict(row)
    ats_type, ats_slug = detect_ats(record["apply_url"])
    record["ats_type"] = ats_type
    record["ats_slug"] = ats_slug
    record["next_states"] = states.allowed_from(record["state"])
    record["is_terminal"] = record["state"] in states.TERMINAL
    return record


def state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["state"]: row["n"]
        for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM applications GROUP BY state"
        )
    }


def events_for(conn: sqlite3.Connection, application_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM events
         WHERE application_id = ?
         ORDER BY occurred_at ASC, id ASC
        """,
        (application_id,),
    ).fetchall()


def set_would_apply_anyway(
    conn: sqlite3.Connection, application_id: int, value: int
) -> None:
    """The honesty flag. Not a state change, so it does not go through transition()."""
    if value not in (0, 1):
        raise ValueError("would_apply_anyway is 0 or 1")
    with transaction(conn):
        conn.execute(
            "UPDATE applications SET would_apply_anyway = ?, updated_at = ? WHERE id = ?",
            (value, config.utcnow(), application_id),
        )


# ================================== contacts ==================================


def list_contacts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT ct.*, c.name AS company_name
          FROM contacts ct
          LEFT JOIN companies c ON c.id = ct.company_id
         WHERE ct.do_not_contact = 0
         ORDER BY ct.name
        """
    ).fetchall()


def contacts_at_company(conn: sqlite3.Connection, company_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contacts WHERE company_id = ? AND do_not_contact = 0",
        (company_id,),
    ).fetchall()


# =================================== stats ===================================
#
# ATS is derived by regex on read, so it cannot be a SQL GROUP BY. At ~100
# applications a month the whole table is a trivial scan, and aggregating in
# Python keeps one implementation of ATS detection instead of two.


def _blank_bucket(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "tracked": 0,
        "applied": 0,
        "responses": 0,
        "interviews": 0,
        "offers": 0,
        "rejections": 0,
        "ghosted": 0,
    }


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    applied = bucket["applied"]
    bucket["response_rate"] = bucket["responses"] / applied if applied else None
    bucket["interview_rate"] = bucket["interviews"] / applied if applied else None
    return bucket


def conversion_by(conn: sqlite3.Connection, dimension: str) -> list[dict[str, Any]]:
    """Funnel counts bucketed by `ats`, `source`, or `referral`.

    `applied` counts every application that reached `applied`, including ones
    that have since moved on — conversion of submissions, not of current state.
    """
    rows = conn.execute(
        """
        SELECT a.id, a.state, a.applied_at, a.first_response_at, a.referral_contact_id,
               j.apply_url, j.source
          FROM applications a
          JOIN jobs j ON j.id = a.job_id
        """
    ).fetchall()

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        if dimension == "ats":
            key = detect_ats(row["apply_url"])[0] or "other / direct"
        elif dimension == "source":
            key = row["source"] or "unknown"
        elif dimension == "referral":
            key = "referred" if row["referral_contact_id"] else "cold"
        else:
            raise ValueError(f"unknown dimension {dimension!r}")

        bucket = buckets.setdefault(key, _blank_bucket(key))
        bucket["tracked"] += 1
        if row["applied_at"]:
            bucket["applied"] += 1
        if row["first_response_at"]:
            bucket["responses"] += 1
        if row["state"] in (states.INTERVIEW, states.OFFER):
            bucket["interviews"] += 1
        if row["state"] == states.OFFER:
            bucket["offers"] += 1
        if row["state"] == states.REJECTED:
            bucket["rejections"] += 1
        if row["state"] == states.APPLIED and not row["first_response_at"]:
            bucket["ghosted"] += 1

    ordered = sorted(buckets.values(), key=lambda b: (-b["applied"], b["label"]))
    return [_finish(bucket) for bucket in ordered]


def honesty(conn: sqlite3.Connection) -> dict[str, Any]:
    """The drift metric. A falling ratio means the system is manufacturing volume."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS answered,
               SUM(would_apply_anyway) AS yes
          FROM applications
         WHERE would_apply_anyway IS NOT NULL
        """
    ).fetchone()
    unanswered = conn.execute(
        """
        SELECT COUNT(*) AS n FROM applications
         WHERE applied_at IS NOT NULL AND would_apply_anyway IS NULL
        """
    ).fetchone()["n"]
    answered = row["answered"] or 0
    yes = row["yes"] or 0
    return {
        "answered": answered,
        "yes": yes,
        "ratio": (yes / answered) if answered else None,
        "unanswered": unanswered,
    }


def queue_health(conn: sqlite3.Connection) -> dict[str, Any]:
    """Queue rot watch: a growing `job_approved` backlog means I am not applying."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS backlog, MIN(updated_at) AS oldest
          FROM applications WHERE state IN (?, ?)
        """,
        (states.JOB_APPROVED, states.PACKET_READY),
    ).fetchone()
    applied_30d = conn.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE applied_at >= ?",
        (config.days_ago(30),),
    ).fetchone()["n"]
    return {
        "backlog": row["backlog"],
        "oldest": row["oldest"],
        "applied_30d": applied_30d,
    }
