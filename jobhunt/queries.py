"""Thin query module. Parameterized SQL in named functions. No ORM.

State writes are not here — they live in states.py, which is the only place
`applications.state` is written.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import config, states
from .db import transaction
from .normalize import detect_ats, norm_company_name, norm_title, normalize_apply_url

# Bucket label for an apply URL whose host matches no known ATS. Shared by the
# stats page and the ATS filter so the two never disagree.
UNKNOWN_ATS = "other / direct"

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


# UNWIRED. This is the soft dedup from docs/architecture.md — same company, same
# normalized title, same location — for one posting listed under two aggregator URLs.
# The entry form currently catches only the hard `apply_url_norm` collision, so that
# case double-tracks. Wire this into the entry form and into `ingest` (Phase 4).
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
    """Insert a job. `apply_url_norm` is UNIQUE — a duplicate raises IntegrityError.

    `posted_at` is validated here rather than in the route: Phase 4 ingest writes
    this column through this function without passing through the web layer, so a
    check up there would guard the path that matters least.
    """
    posted_at = config.to_utc_timestamp(posted_at) if posted_at else None
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
# Explicit rather than `a.*`, to keep three large columns out of every row.
# `resume_pdf` is ~30 KB, and the pipeline table selected it for every listed
# application to answer a question a boolean answers. `resume_data` and
# `answers_json` are the same shape of waste. The routes that genuinely need
# the bytes — the PDF download and the packet diff — query for them directly.
_APPLICATION_SELECT = """
SELECT a.id, a.job_id, a.state, a.score, a.score_reasoning,
       a.referral_contact_id, a.would_apply_anyway, a.applied_at,
       a.first_response_at, a.outcome, a.created_at, a.updated_at,
       a.resume_pdf IS NOT NULL  AS has_resume,
       a.answers_json IS NOT NULL AS has_answers,
       j.title, j.location, j.remote, j.apply_url, j.apply_url_norm, j.source,
       j.comp_min, j.comp_max, j.posted_at, j.discovered_at,
       -- Not `j.jd_text`. Nothing that reads these rows renders a description —
       -- the packet, scoring and tailoring each fetch it themselves — and at 4.7 KB
       -- a row it was 2.4 MB of a 2.9 MB pipeline response, invisible while the
       -- server rendered the HTML and ruinous the moment a phone had to download it.
       length(j.jd_text) > 0 AS has_jd,
       c.id AS company_id, c.name AS company_name,
       ct.name AS referral_name, ct.channel AS referral_channel,
       ct.handle AS referral_handle
  FROM applications a
  JOIN jobs j      ON j.id = a.job_id
  JOIN companies c ON c.id = j.company_id
  LEFT JOIN contacts ct ON ct.id = a.referral_contact_id
"""


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
    # SQLite has no boolean, so these arrive as 0/1. Coerce here rather than
    # leaving every caller to remember. `would_apply_anyway` is deliberately NOT
    # in this list: None means unanswered, which is a third state the UI shows.
    for flag in ("has_resume", "has_answers", "has_jd"):
        if flag in record:
            record[flag] = bool(record[flag])
    return record


# ============================ filtering / sorting ============================
#
# The applications table is filtered in SQL where the column exists, and in
# Python where it does not: `ats` is regexed from the apply URL on read. Sorting
# is entirely in Python so that one rule holds for every column — rows with
# nothing to sort by trail the list in both directions, instead of flipping to
# the top on a descending sort the way SQL NULLs do.

# Column key in the URL -> header label. Order matches the table's cells.
SORTABLE: dict[str, str] = {
    "company": "Company",
    "role": "Role",
    "state": "State",
    "ats": "ATS",
    "source": "Source",
    "applied": "Applied",
    "ref": "Ref",
    "waa": "WAA",
}

# Sorting by state follows the pipeline, not the alphabet: the column is a
# lifecycle, so `discovered` before `applied` is the only ordering that means
# anything.
STATE_RANK: dict[str, int] = {
    state: rank
    for rank, state in enumerate(
        list(states.PIPELINE_ORDER)
        + sorted(states.TERMINAL.difference(states.PIPELINE_ORDER))
    )
}


def _sort_value(record: dict[str, Any], column: str) -> Any:
    """The comparable value for one column, or None when there is nothing to sort."""
    if column == "company":
        return (record["company_name"] or "").lower() or None
    if column == "role":
        return (record["title"] or "").lower() or None
    if column == "state":
        return STATE_RANK.get(record["state"])
    if column == "ats":
        return record["ats_type"]
    if column == "source":
        return (record["source"] or "").lower() or None
    if column == "applied":
        return record["applied_at"]
    if column == "ref":
        return (record["referral_name"] or "").lower() or None
    if column == "waa":
        return record["would_apply_anyway"]
    raise ValueError(f"column {column!r} is not sortable")


def sort_records(
    records: list[dict[str, Any]], column: str, direction: str
) -> list[dict[str, Any]]:
    """Rows with a value first, in `direction`; rows with none trail, newest first.

    The key is computed once per row rather than three times — the two partition
    passes and the sort each called `_sort_value` again, which for the whole
    pipeline table meant ~16,500 dispatches to answer 5,500 questions.
    """
    keyed = [(_sort_value(record, column), record) for record in records]
    ranked = [pair for pair in keyed if pair[0] is not None]
    unranked = [record for value, record in keyed if value is None]
    # `key=` compares the sort value alone, never the dict beside it — which
    # would raise the moment two rows tied.
    ranked.sort(key=lambda pair: pair[0], reverse=direction == "desc")
    # Tie-break and trailing group both fall back to most recent first.
    unranked.sort(key=lambda r: r["id"], reverse=True)
    return [record for _, record in ranked] + unranked


def filter_applications(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    state: str = "",
    ats: str = "",
    source: str = "",
    referral: str = "",
    waa: str = "",
    sort: str = "applied",
    direction: str = "desc",
    limit: int = 500,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []

    if state:
        where.append("a.state = ?")
        params.append(state)
    if source:
        where.append("j.source = ?")
        params.append(source)
    if referral == "referred":
        where.append("a.referral_contact_id IS NOT NULL")
    elif referral == "cold":
        where.append("a.referral_contact_id IS NULL")
    if waa == "yes":
        where.append("a.would_apply_anyway = 1")
    elif waa == "no":
        where.append("a.would_apply_anyway = 0")
    elif waa == "unanswered":
        where.append("a.would_apply_anyway IS NULL")
    if q:
        where.append(
            "(c.name LIKE ? OR j.title LIKE ? OR IFNULL(j.location, '') LIKE ?)"
        )
        params.extend([f"%{q}%"] * 3)

    sql = _APPLICATION_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    records = [_decorate(row) for row in conn.execute(sql, params)]

    if ats:
        records = [r for r in records if (r["ats_type"] or UNKNOWN_ATS) == ats]

    # Sort before slicing, or the cap would silently change what the sort means.
    return sort_records(records, sort, direction)[:limit]


def facets(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Values actually present, so the filters never offer an empty result."""
    rows = conn.execute(
        "SELECT a.state, j.source, j.apply_url FROM applications a JOIN jobs j ON j.id = a.job_id"
    ).fetchall()
    return {
        "states": sorted(
            {row["state"] for row in rows}, key=lambda s: STATE_RANK.get(s, 99)
        ),
        "sources": sorted({row["source"] for row in rows if row["source"]}),
        "ats": sorted({detect_ats(row["apply_url"])[0] or UNKNOWN_ATS for row in rows}),
    }


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
    # `interviews` and `offers` are derived from `events`, not from current state.
    # An application that interviewed and was then rejected sits in state `rejected`
    # (docs/architecture.md documents interview -> rejected), so counting current
    # state silently drops it — and drops MORE of them the longer the pipeline runs,
    # because interviews keep resolving. Since interviews-per-submission is the stated
    # objective, that number has to come from history. `events` is that history.
    rows = conn.execute(
        """
        SELECT a.id, a.state, a.applied_at, a.first_response_at, a.referral_contact_id,
               j.apply_url, j.source,
               EXISTS (SELECT 1 FROM events e
                        WHERE e.application_id = a.id AND e.to_state = 'interview')
                 AS ever_interviewed,
               EXISTS (SELECT 1 FROM events e
                        WHERE e.application_id = a.id AND e.to_state = 'offer')
                 AS ever_offered
          FROM applications a
          JOIN jobs j ON j.id = a.job_id
        """
    ).fetchall()

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        if dimension == "ats":
            key = detect_ats(row["apply_url"])[0] or UNKNOWN_ATS
        elif dimension == "source":
            key = row["source"] or "unknown"
        elif dimension == "referral":
            key = "referred" if row["referral_contact_id"] else "cold"
        elif dimension == "all":
            key = "all"
        else:
            raise ValueError(f"unknown dimension {dimension!r}")

        bucket = buckets.setdefault(key, _blank_bucket(key))
        bucket["tracked"] += 1
        if row["applied_at"]:
            bucket["applied"] += 1
        if row["first_response_at"]:
            bucket["responses"] += 1
        if row["ever_interviewed"]:
            bucket["interviews"] += 1
        if row["ever_offered"]:
            bucket["offers"] += 1
        if row["state"] == states.REJECTED:
            bucket["rejections"] += 1
        if row["state"] == states.APPLIED and not row["first_response_at"]:
            bucket["ghosted"] += 1

    ordered = sorted(buckets.values(), key=lambda b: (-b["applied"], b["label"]))
    return [_finish(bucket) for bucket in ordered]


# Conversion table columns. `label` is the bucket name, whose header text differs
# per table (ATS / Source / Referral) and is supplied by the caller.
CONVERSION_SORTABLE: dict[str, str] = {
    "label": "",
    "tracked": "Tracked",
    "applied": "Applied",
    "responses": "Responses",
    "interviews": "Interviews",
    "offers": "Offers",
    "ghosted": "Ghosted",
    "response_rate": "Response rate",
    "interview_rate": "Interview rate",
}

STATE_TABLE_SORTABLE: dict[str, str] = {"state": "State", "count": "Applications"}


def sort_buckets(
    buckets: list[dict[str, Any]], column: str, direction: str
) -> list[dict[str, Any]]:
    """Sort conversion rows, same rule as the applications table.

    A rate is None for a bucket nothing was submitted to; those rows trail in
    both directions rather than claiming to be the best or worst performer.
    """
    if column not in CONVERSION_SORTABLE:
        column = "applied"
    key = (
        (lambda b: b["label"].lower())
        if column == "label"
        else (lambda b: b[column])
    )
    ranked = [b for b in buckets if b[column] is not None]
    unranked = [b for b in buckets if b[column] is None]
    ranked.sort(key=key, reverse=direction == "desc")
    unranked.sort(key=lambda b: b["label"].lower())
    return ranked + unranked


def state_rows(
    conn: sqlite3.Connection, column: str = "state", direction: str = "asc"
) -> list[dict[str, Any]]:
    """Every state with its count, sorted by pipeline position or by count."""
    counts = state_counts(conn)
    rows = [
        {"state": state, "count": counts.get(state, 0), "rank": rank}
        for state, rank in STATE_RANK.items()
    ]
    key = (
        (lambda r: r["rank"]) if column != "count" else (lambda r: (r["count"], -r["rank"]))
    )
    rows.sort(key=key, reverse=direction == "desc")
    return rows


def overall(conn: sqlite3.Connection) -> dict[str, Any]:
    """The whole funnel, ungrouped. Interview rate here is the headline metric —
    the objective is interviews per hand-submitted application."""
    buckets = conversion_by(conn, "all")
    return buckets[0] if buckets else _finish(_blank_bucket("all"))


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


# =================================== corpus ===================================
# Read-only views of the profile corpus. `docs/profile/` is the source of truth
# and these rows are derived from it by `make load-profile`; nothing here writes.


def profile_facts(conn: sqlite3.Connection) -> dict[str, str]:
    """`profile_facts` as a plain mapping. Keys keep their YAML path."""
    return {
        row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM profile_facts")
    }


def corpus_experiences(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM experiences ORDER BY sort_order, id"
    ).fetchall()


def corpus_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects ORDER BY sort_order, id").fetchall()


def corpus_education(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM education ORDER BY COALESCE(end_month, '9999-99') DESC, id"
    ).fetchall()


def corpus_credentials(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM credentials ORDER BY kind, COALESCE(issued, '') DESC, id"
    ).fetchall()


def corpus_languages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """UNWIRED. The resume dropped its languages section; the loader still fills
    the table, and an answer in Phase 3 is the likely next reader."""
    return conn.execute("SELECT * FROM languages ORDER BY id").fetchall()


def corpus_bullets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every bullet, with its parent. The tailor selects from exactly this set."""
    return conn.execute(
        "SELECT * FROM bullets ORDER BY experience_id, project_id, sort_order, id"
    ).fetchall()


def corpus_stories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Behavioural stories, for the prompts that answer essay questions.

    Deliberately not part of `corpus_bullets`. A resume line must come from
    exactly one `bullets` row and carries that row's id through validation; a
    story has no id and could never survive that check. They answer a different
    kind of question — what happened between people — and go to a different
    set of prompts.
    """
    return conn.execute("SELECT * FROM stories ORDER BY id").fetchall()


def llm_spend(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Cost and cache behaviour over a rolling window.

    The cache hit rate is the point. CLAUDE.md ranks prompt caching as the first
    cost lever, ahead of the Batch API and model tier, and a write bills at 125%
    against a read's 10% — so a task whose calls never land inside the 5-minute
    TTL is paying a premium for an entry it never reads. That looks like a high
    write count with a hit rate near zero, and it is not visible from cost alone.
    """
    row = conn.execute(
        """
        SELECT COUNT(*)                        AS calls,
               COALESCE(SUM(cost_usd), 0)      AS cost,
               COALESCE(SUM(cache_read_tokens), 0)  AS read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS write_tokens,
               SUM(error IS NOT NULL)          AS failed,
               SUM(stop_reason = 'max_tokens') AS truncated
          FROM llm_calls
         WHERE called_at >= ?
        """,
        (config.days_ago(days),),
    ).fetchone()

    cached = (row["read_tokens"] or 0) + (row["write_tokens"] or 0)
    return {
        "days": days,
        "calls": row["calls"],
        "cost": row["cost"],
        "failed": row["failed"] or 0,
        "truncated": row["truncated"] or 0,
        # Share of cacheable tokens that were served from cache rather than
        # written. None, not 0, when nothing cacheable was sent — an unused
        # cache and an absent one are different situations.
        "hit_rate": (row["read_tokens"] / cached) if cached else None,
    }


def bullets_by_id(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    """The source rows a tailored resume claims to be built from."""
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM bullets WHERE id IN ({marks})", tuple(ids)).fetchall()
    return {int(row["id"]): row for row in rows}


# ============================== the web layer's ==============================
#
# SQL that used to sit inline in web/app.py. Moved here when the JSON API landed:
# two callers rendering the same rows is exactly when "parameterized SQL in named
# functions" stops being style and starts being the thing that keeps them honest.


def sibling_application_ids(conn: sqlite3.Connection, application_id: int) -> list[int]:
    """Undecided rows that are the same role at the same company.

    One company listing a role in nine cities is nine rows. They are genuinely
    nine postings — nine apply URLs, nine `apply_url_norm` keys — but one *shot*,
    and exactly one gets submitted.

    `job_approved` is included, not just `scored`: the review queue collapses
    duplicates before they are seen, but siblings approved before that existed —
    or approved from the pipeline table, which does not collapse — sit past
    `scored` where nothing was closing them. `packet_ready` is deliberately
    excluded, because a packet has a rendered resume behind it and retiring one
    is a decision, not a side effect.
    """
    return [
        int(row["id"])
        for row in conn.execute(
            """
            SELECT other.id
              FROM applications a
              JOIN jobs j       ON j.id = a.job_id
              JOIN jobs oj      ON oj.company_id = j.company_id
                               AND oj.title_norm = j.title_norm
              JOIN applications other ON other.job_id = oj.id
             WHERE a.id = ? AND other.id != a.id
               AND other.state IN ('scored', 'job_approved')
            """,
            (application_id,),
        )
    ]


def scored_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everything in `scored`, with the one referral name that matters per row."""
    return conn.execute(
        """
        SELECT a.id AS application_id, a.score, a.score_reasoning,
               j.title, j.title_norm, j.company_id, j.location, j.remote,
               j.apply_url, j.jd_text, j.comp_min, j.comp_max, c.name AS company,
               (SELECT ct.name FROM contacts ct
                 WHERE ct.company_id = c.id AND ct.do_not_contact = 0
                 ORDER BY ct.id LIMIT 1) AS referral
          FROM applications a
          JOIN jobs j      ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.state = ?
        """,
        (states.SCORED,),
    ).fetchall()


def packet_row(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    """Posting fields and stored resume JSON for one packet view."""
    return conn.execute(
        """
        SELECT j.apply_url, j.jd_text, j.location, j.remote, a.resume_data,
               a.resume_findings,
               (SELECT ct.name FROM contacts ct
                 WHERE ct.company_id = j.company_id AND ct.do_not_contact = 0
                 ORDER BY ct.id LIMIT 1) AS referral
          FROM applications a JOIN jobs j ON j.id = a.job_id
         WHERE a.id = ?
        """,
        (application_id,),
    ).fetchone()


def duplicate_listings(conn: sqlite3.Connection, application_id: int) -> list[sqlite3.Row]:
    """Same role, other cities, in any live state. Shown, never acted on."""
    return conn.execute(
        """
        SELECT other.id, other.state, oj.location
          FROM applications a
          JOIN jobs j  ON j.id = a.job_id
          JOIN jobs oj ON oj.company_id = j.company_id AND oj.title_norm = j.title_norm
          JOIN applications other ON other.job_id = oj.id
         WHERE a.id = ? AND other.id != a.id
           AND other.state NOT IN ('skipped', 'filtered', 'expired')
         ORDER BY other.id
        """,
        (application_id,),
    ).fetchall()


def company_id_for_application(conn: sqlite3.Connection, application_id: int) -> int | None:
    row = conn.execute(
        "SELECT j.company_id FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?",
        (application_id,),
    ).fetchone()
    return None if row is None else int(row["company_id"])


def answer_context(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    """Company, title and JD — what drafting one narrative answer needs."""
    return conn.execute(
        """
        SELECT j.company_id, j.title, j.jd_text, c.name AS company
          FROM applications a JOIN jobs j ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.id = ?
        """,
        (application_id,),
    ).fetchone()


def resume_pdf_bytes(conn: sqlite3.Connection, application_id: int) -> bytes | None:
    """The stored bytes, never a re-render — see .claude/rules/data-layer.md."""
    row = conn.execute(
        "SELECT resume_pdf FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    return None if row is None else row["resume_pdf"]


def resume_docx_bytes(conn: sqlite3.Connection, application_id: int) -> bytes | None:
    """The stored .docx master bytes, for `.docx`-on-request. Frozen, never re-rendered."""
    row = conn.execute(
        "SELECT resume_docx FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    return None if row is None else row["resume_docx"]


def corpus_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "bullets": conn.execute("SELECT COUNT(*) AS n FROM bullets").fetchone()["n"],
        "experiences": conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"],
        "projects": conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"],
    }


# ============================== resume library ==============================
# Readers for the select-from-a-library engine (Phase 1). Parallel to corpus_*;
# the old corpus readers above are untouched until every consumer has moved over.


def _resume_json(value: Any) -> Any:
    """Parse a JSON column, tolerating NULL and malformed content."""
    try:
        return json.loads(value) if value else None
    except (json.JSONDecodeError, TypeError):
        return None


def resume_bullets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every library bullet, JSON columns parsed, keyed usably for the selector."""
    out: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM resume_bullets ORDER BY sort_order, id"):
        d = dict(row)
        d["framing"] = _resume_json(row["framing"])
        for key in ("tags", "must_keep", "no_add", "no_upgrade"):
            d[key] = _resume_json(row[key]) or []
        out.append(d)
    return out


def resume_variants(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for row in conn.execute("SELECT * FROM resume_variants ORDER BY sort_order"):
        d = dict(row)
        d["skills_order"] = _resume_json(row["skills_order"]) or []
        out.append(d)
    return out


def resume_variant_entries(conn: sqlite3.Connection, variant: str) -> list[dict[str, Any]]:
    out = []
    for row in conn.execute(
        "SELECT * FROM resume_variant_entries WHERE variant = ? ORDER BY sort_order",
        (variant,),
    ):
        d = dict(row)
        d["default_bullets"] = _resume_json(row["default_bullets"]) or []
        out.append(d)
    return out


def resume_summaries(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["text"] for r in conn.execute("SELECT key, text FROM resume_summaries")}


def resume_skill_groups(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """Grouped skills in order: `[(group_name, [skill, ...]), ...]`."""
    out: list[tuple[str, list[str]]] = []
    for group in conn.execute("SELECT name FROM resume_skill_groups ORDER BY sort_order"):
        skills = [
            r["skill"]
            for r in conn.execute(
                "SELECT skill FROM resume_skills WHERE group_name = ? ORDER BY sort_order",
                (group["name"],),
            )
        ]
        out.append((group["name"], skills))
    return out


def resume_education(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM resume_education ORDER BY sort_order")]


def resume_meta(conn: sqlite3.Connection, key: str) -> Any:
    """A structured config value the library carries — title_default, open_facts,
    optional_education_line, gpa — parsed from JSON. None when absent."""
    row = conn.execute("SELECT value FROM resume_meta WHERE key = ?", (key,)).fetchone()
    return _resume_json(row["value"]) if row else None


def resume_swap_entries(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Swap-in project entries (PEP/FPS) keyed by entry_key, for the renderer."""
    return {r["entry_key"]: dict(r) for r in conn.execute("SELECT * FROM resume_swap_entries")}


def job_description(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    """The whole JD for one application.

    Its own query because the review batch ships an excerpt: eight full
    descriptions is most of a megabyte on a phone, and seven of them are never
    opened. This is the one you asked to read.
    """
    return conn.execute(
        """
        SELECT j.title, j.jd_text, j.apply_url, c.name AS company
          FROM applications a
          JOIN jobs j      ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.id = ?
        """,
        (application_id,),
    ).fetchone()


def unknown_question_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM unknown_questions").fetchone()["n"])


def application_for_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """The application already tracking this job, for the duplicate URL check."""
    return conn.execute(
        "SELECT id, state FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """`sqlite3.Row` is not JSON-serializable. Convert at the boundary, once."""
    return [dict(row) for row in rows]
