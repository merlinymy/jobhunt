"""View models: the shape each page needs, with no framework in it.

Two renderers read these — the Jinja templates and the JSON API — so anything
that decides *what* is shown lives here, and both callers agree by construction.
Nothing in this module imports FastAPI, touches a `Request`, or knows what HTMX
is. Query parameters arrive as a plain mapping; a missing row raises `LookupError`
and the caller turns that into whatever a 404 looks like in its world.

The URL fragments on sort headers are built here too. React does not use them —
it drives its own query string — but one implementation that carries a few spare
bytes beats two implementations of a rule about which direction a column sorts
first.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from .. import answers, config, prefilter, queries, runs, states

# ---------------------------------------------------------------- pipeline ---

FILTER_FIELDS = ("q", "state", "ats", "source", "referral", "waa")
DEFAULT_SORT = "applied"
DEFAULT_DIRECTION = "desc"
# Columns whose most useful first click is newest/highest first.
DESCENDING_FIRST = frozenset({"applied", "waa", "state"})

REVIEW_LIMIT = 8
JD_EXCERPT = 1200

# Conversion tables on the stats page: URL prefix -> the bucket column's header.
STATS_TABLES = {"ats": "ATS", "source": "Source", "referral": "Referral"}
# Each table sorts independently, so its parameters are namespaced by prefix.
STATS_DEFAULTS = {"ats": ("applied", "desc"), "source": ("applied", "desc"),
                  "referral": ("applied", "desc"), "states": ("state", "asc")}

_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def dead_ends() -> list[str]:
    """Terminal states that are not steps in the pipeline.

    `offer` is both terminal and the end of the pipeline; only show it once.
    """
    return sorted(states.TERMINAL.difference(states.PIPELINE_ORDER))


def table_view(conn: sqlite3.Connection, params: Mapping[str, str]) -> dict[str, Any]:
    """Filter/sort state read from the query string, plus everything to render it."""
    filters = {name: (params.get(name) or "").strip() for name in FILTER_FIELDS}

    sort = params.get("sort") or DEFAULT_SORT
    if sort not in queries.SORTABLE:
        sort = DEFAULT_SORT
    direction = params.get("dir") if params.get("dir") in ("asc", "desc") else DEFAULT_DIRECTION

    active = {name: value for name, value in filters.items() if value}
    headers = []
    for column, label in queries.SORTABLE.items():
        is_active = column == sort
        if is_active:
            next_direction = "asc" if direction == "desc" else "desc"
        else:
            next_direction = "desc" if column in DESCENDING_FIRST else "asc"
        headers.append({
            "column": column,
            "label": label,
            "active": is_active,
            "direction": direction if is_active else None,
            "next_direction": next_direction,
            "query": urlencode({**active, "sort": column, "dir": next_direction}),
        })

    # Funnel steps are the state filter in disguise. They keep the other filters
    # and the sort, and clicking the active step clears the state rather than
    # re-applying it — the step doubles as the way back out.
    others = {name: value for name, value in active.items() if name != "state"}
    ordering = {"sort": sort, "dir": direction}
    funnel = {
        step: urlencode(
            {**others, **ordering}
            if step == filters["state"]
            else {**others, "state": step, **ordering}
        )
        for step in list(states.PIPELINE_ORDER) + dead_ends()
    }

    return {
        "applications": queries.filter_applications(
            conn, sort=sort, direction=direction, **filters
        ),
        "headers": headers,
        "filters": filters,
        "sort": sort,
        "direction": direction,
        "active_filters": len(active),
        "facets": queries.facets(conn),
        "funnel": funnel,
        "clear_query": urlencode(ordering),
    }


def pipeline_view(conn: sqlite3.Connection, params: Mapping[str, str]) -> dict[str, Any]:
    """The whole pipeline page: the table plus the panels above it."""
    return {
        "counts": queries.state_counts(conn),
        "health": queries.queue_health(conn),
        "honesty": queries.honesty(conn),
        **table_view(conn, params),
    }


def state_panel(conn: sqlite3.Connection, application_id: int) -> dict[str, Any]:
    """One application plus its event history — what a transition swaps."""
    record = queries.get_application(conn, application_id)
    if record is None:
        raise LookupError("no such application")
    return {
        "app": record,
        "events": queries.rows_to_dicts(queries.events_for(conn, application_id)),
    }


def detail_view(conn: sqlite3.Connection, application_id: int) -> dict[str, Any]:
    panel = state_panel(conn, application_id)
    company_id = panel["app"].get("company_id")
    referrals = (
        queries.contacts_at_company(conn, int(company_id)) if company_id else []
    )
    return {**panel, "referrals": queries.rows_to_dicts(referrals)}


# ------------------------------------------------------------------ review ---


def review_batch(conn: sqlite3.Connection, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Top `limit` scored postings, plus how many are waiting in total.

    Collapsed on `(company_id, title_norm)`. Measured on a real run: 160 of 661
    scored rows were the same role relisted per metro — 24% of the queue — and
    six of the first eight cards were one Clera founding-engineer posting in
    different cities. Showing eight cards that are really three is not a queue.

    Nothing is deleted or hidden from the DB; the pipeline table still has every
    row. This picks the best-located member of each group to show, which is why
    the sort runs before the grouping rather than after.
    """
    rows = queries.scored_candidates(conn)
    cfg = prefilter.load()
    ranked = sorted(
        rows, key=lambda r: (prefilter.location_tier(r, cfg), -(r["score"] or 0))
    )

    # Sorted first, so the survivor of each group is the best-located one — the
    # remote listing of a role wins over the Houston one, which is the whole
    # point of ranking location.
    seen: dict[tuple[int, str], int] = {}
    collapsed = []
    for row in ranked:
        key = (int(row["company_id"]), row["title_norm"] or row["title"])
        if key in seen:
            seen[key] += 1
            continue
        seen[key] = 0
        collapsed.append(row)

    batch = []
    for row in collapsed[:limit]:
        jd = (row["jd_text"] or "").strip()
        batch.append({
            "application_id": int(row["application_id"]),
            "title": row["title"],
            "company": row["company"],
            "where": _where(row["location"], row["remote"]),
            "score": row["score"] or 0,
            "reason": row["score_reasoning"] or "",
            "apply_url": row["apply_url"],
            "comp": _comp_band(row["comp_min"], row["comp_max"]),
            "referral": row["referral"],
            "excerpt": (jd[:JD_EXCERPT] + "…") if len(jd) > JD_EXCERPT else jd,
            "also_in": seen[(int(row["company_id"]), row["title_norm"] or row["title"])],
        })
    return batch, len(collapsed)


def _where(location: str | None, remote: str | None) -> str:
    where = location or "—"
    if (remote or "").lower() == "remote":
        return f"Remote · {location}" if location else "Remote"
    return where


def _comp_band(low: int | None, high: int | None) -> str | None:
    if low and high:
        return f"${low // 1000}k–${high // 1000}k"
    if low:
        return f"from ${low // 1000}k"
    if high:
        return f"up to ${high // 1000}k"
    return None


# ------------------------------------------------------------------ packet ---


def packet_view(conn: sqlite3.Connection, application_id: int) -> dict[str, Any]:
    record = queries.get_application(conn, application_id)
    if record is None:
        raise LookupError("no such application")
    row = queries.packet_row(conn, application_id)
    if row is None:
        raise LookupError("no such application")

    # The diff is rebuilt from `resume_data` — the exact document that produced
    # the stored bytes — rather than re-tailoring. Re-running the model would
    # show a diff against a resume that was never downloaded.
    diff: list[dict[str, Any]] = []
    pdf_meta = None
    if row["resume_data"]:
        try:
            document = json.loads(row["resume_data"])
        except json.JSONDecodeError:
            document = {}
        selected: list[str] = []
        for entries in (document.get("cv", {}).get("sections", {}) or {}).values():
            for entry in entries:
                if isinstance(entry, dict):
                    selected.extend(entry.get("highlights", []) or [])
        sources = {b["text"]: b for b in queries.corpus_bullets(conn)}
        for text in selected:
            source = sources.get(text)
            diff.append({
                "bullet_id": int(source["id"]) if source else None,
                "before": source["text"] if source else "(reworded — see the PDF)",
                "after": text,
                "changed": source is None,
            })
        pdf_meta = {
            "bullets": len(selected),
            "reworded": sum(1 for d in diff if d["changed"]),
        }

    duplicate_rows = queries.duplicate_listings(conn, application_id)
    return {
        "app": record,
        "where": _where(row["location"], row["remote"]),
        "duplicates": len(duplicate_rows),
        "duplicate_rows": queries.rows_to_dicts(duplicate_rows),
        "apply_url": row["apply_url"],
        "referral": row["referral"],
        "has_jd": bool((row["jd_text"] or "").strip()),
        "diff": diff,
        "pdf": pdf_meta,
        # None = never built. [] = built and nothing objected, which is a claim
        # worth making rather than the same silence as "not checked".
        "findings": _findings(row["resume_findings"]),
        "error": None,
    }


def _findings(raw: str | None) -> list[dict[str, Any]] | None:
    """`applications.resume_findings` -> what the packet page shows.

    Tolerant on purpose: a row written before the column existed, or by a build
    that crashed mid-write, should render a packet with no annotations rather
    than 500 the page that exists to show you the resume.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def answers_view(conn: sqlite3.Connection, application_id: int) -> dict[str, Any]:
    company_id = queries.company_id_for_application(conn, application_id)
    if company_id is None:
        raise LookupError("no such application")
    return {
        "app": {"id": application_id},
        "answers": answers.resolve_all(conn, company_id),
        "company_id": company_id,
        "unknowns": queries.unknown_question_count(conn),
    }


def answers_json(resolved: list[Any]) -> list[dict[str, Any]]:
    """Serialize `answers.Resolved` by hand.

    `missing` is a `@property`, and `jsonable_encoder` only emits dataclass
    *fields* — so handing these straight to FastAPI would silently drop the one
    piece the UI branches on.
    """
    return [
        {
            "key": item.question.key,
            "text": item.question.text,
            "tier": item.question.tier,
            "answer": item.answer,
            "source": item.source,
            "missing": item.missing,
            "optional": getattr(item.question, "optional", False),
        }
        for item in resolved
    ]


# ------------------------------------------------------------------- fill ---


def date_forms(month: str | None) -> dict[str, str] | None:
    """`YYYY-MM` in every shape an ATS asks for, because they disagree.

    Workday wants a month name or number from a dropdown and the year separately,
    Greenhouse takes `MM/YYYY`, a few want the ISO month. Rendering all of them
    beats retyping or doing the conversion in your head at 11pm.
    """
    if not month:
        return None
    parts = str(month).strip().split("-")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    year, number = parts[0], int(parts[1])
    if not 1 <= number <= 12:
        return None
    return {
        "iso": f"{year}-{number:02d}",
        "slash": f"{number:02d}/{year}",
        "month": f"{number:02d}",
        "month_name": _MONTHS[number - 1],
        "year": year,
    }


def _described(rows: list[sqlite3.Row]) -> tuple[list[str], str]:
    """Bullets, and the one block that goes in a Description textarea."""
    bullets = [row["text"] for row in rows]
    return bullets, "\n".join(f"• {text}" for text in bullets)


IDENTITY_FIELDS = (
    ("Legal name", "identity.legal_name"),
    ("Preferred name", "identity.preferred_name"),
    ("Email", "identity.email"),
    ("Phone", "identity.phone"),
    ("City", "identity.city"),
    ("State", "identity.state"),
    ("LinkedIn", "identity.linkedin"),
    ("GitHub", "identity.github"),
    ("Website", "identity.website"),
)


def fill_view(conn: sqlite3.Connection) -> dict[str, Any]:
    facts = queries.profile_facts(conn)
    identity = [
        {"label": label, "value": facts[key]}
        for label, key in IDENTITY_FIELDS
        if facts.get(key)
    ]

    by_experience: dict[int, list[sqlite3.Row]] = {}
    by_project: dict[int, list[sqlite3.Row]] = {}
    for row in queries.corpus_bullets(conn):
        if row["experience_id"] is not None:
            by_experience.setdefault(int(row["experience_id"]), []).append(row)
        else:
            by_project.setdefault(int(row["project_id"]), []).append(row)

    experiences = []
    for row in queries.corpus_experiences(conn):
        items, block = _described(by_experience.get(int(row["id"]), []))
        experiences.append({
            "company": row["company"], "title": row["title"],
            "location": row["location"], "employment_type": row["employment_type"],
            "start": date_forms(row["start_month"]), "end": date_forms(row["end_month"]),
            "bullets": items, "description": block,
        })

    projects = []
    for row in queries.corpus_projects(conn):
        items, block = _described(by_project.get(int(row["id"]), []))
        projects.append({
            "name": row["name"], "url": row["url"], "role": row["role"],
            "start": date_forms(row["start_month"]), "end": date_forms(row["end_month"]),
            "bullets": items, "description": block,
        })

    education = [
        {"school": row["school"], "degree": row["degree"], "field": row["field"],
         "start": date_forms(row["start_month"]), "end": date_forms(row["end_month"])}
        for row in queries.corpus_education(conn)
    ]

    return {"identity": identity, "experiences": experiences,
            "projects": projects, "education": education}


# ------------------------------------------------------------------ stats ---


def stats_headers(
    prefix: str,
    columns: dict[str, str],
    sort: str,
    direction: str,
    every_param: dict[str, str],
    numeric_from: int = 1,
) -> list[dict[str, Any]]:
    headers = []
    for index, (column, label) in enumerate(columns.items()):
        is_active = column == sort
        if is_active:
            next_direction = "asc" if direction == "desc" else "desc"
        else:
            # Counts and rates are most useful highest-first; names A–Z.
            next_direction = "desc" if index >= numeric_from else "asc"
        headers.append({
            "column": column,
            "label": label,
            "active": is_active,
            "direction": direction if is_active else None,
            "next_direction": next_direction,
            "numeric": index >= numeric_from,
            "query": urlencode(
                {**every_param, f"{prefix}_sort": column, f"{prefix}_dir": next_direction}
            ),
        })
    return headers


def stats_tables(conn: sqlite3.Connection, params: Mapping[str, str]) -> dict[str, Any]:
    ordering: dict[str, tuple[str, str]] = {}
    for prefix, (default_sort, default_direction) in STATS_DEFAULTS.items():
        allowed = (
            queries.STATE_TABLE_SORTABLE if prefix == "states" else queries.CONVERSION_SORTABLE
        )
        sort = params.get(f"{prefix}_sort") or default_sort
        if sort not in allowed:
            sort = default_sort
        direction = params.get(f"{prefix}_dir")
        if direction not in ("asc", "desc"):
            direction = default_direction
        ordering[prefix] = (sort, direction)

    # Every header link carries all four tables' ordering, so a pushed URL
    # reloads into exactly the view on screen.
    every_param: dict[str, str] = {}
    for prefix, (sort, direction) in ordering.items():
        every_param[f"{prefix}_sort"] = sort
        every_param[f"{prefix}_dir"] = direction

    tables: dict[str, Any] = {}
    for prefix, bucket_label in STATS_TABLES.items():
        sort, direction = ordering[prefix]
        columns = {**queries.CONVERSION_SORTABLE, "label": bucket_label}
        tables[prefix] = {
            "prefix": prefix,
            "rows": queries.sort_buckets(queries.conversion_by(conn, prefix), sort, direction),
            "sort": sort,
            "direction": direction,
            "headers": stats_headers(prefix, columns, sort, direction, every_param),
        }

    sort, direction = ordering["states"]
    tables["states"] = {
        "prefix": "states",
        "rows": queries.state_rows(conn, sort, direction),
        "sort": sort,
        "direction": direction,
        "headers": stats_headers(
            "states", queries.STATE_TABLE_SORTABLE, sort, direction, every_param
        ),
    }
    return {"tables": tables}


def stats_view(conn: sqlite3.Connection, params: Mapping[str, str]) -> dict[str, Any]:
    return {
        "overall": queries.overall(conn),
        "honesty": queries.honesty(conn),
        "health": queries.queue_health(conn),
        "spend": queries.llm_spend(conn),
        **stats_tables(conn, params),
    }


# ------------------------------------------------------------------- runs ---


def runs_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the discovery button shows: the live run, and the last of each task.

    Polled every few seconds while something is running, so it stays three
    indexed reads. The last run of each task is what the idle line reads —
    "last sweep 6h ago · 23 new" — and it is also the only place a failure from
    an unattended 06:30 agent is visible without opening a log.

    `waiting_to_score` is the count behind "Score only, 34 waiting": a button
    that spends money should say how much work it is about to buy.
    """
    counts = queries.state_counts(conn)
    return {
        **runs.snapshot(conn),
        "phase_labels": runs.PHASE_LABELS,
        "waiting_to_score": counts.get(states.DISCOVERED, 0),
        "waiting_to_review": counts.get(states.SCORED, 0),
    }


# ------------------------------------------------------------------- meta ---


def meta() -> dict[str, Any]:
    """The vocabulary the client would otherwise hardcode.

    State names, their order, and which transitions are legal all live in
    `states.py`. Shipping them means a new state appears in the UI without a
    second edit, and `skipped` cannot quietly gain an "undo" the server refuses.
    """
    return {
        "pipeline_order": list(states.PIPELINE_ORDER),
        "dead_ends": dead_ends(),
        "terminal": sorted(states.TERMINAL),
        "transitions": {
            state: sorted(states.allowed_from(state))
            for state in sorted(set(states.PIPELINE_ORDER) | states.TERMINAL)
        },
        "sortable": queries.SORTABLE,
        "descending_first": sorted(DESCENDING_FIRST),
        "conversion_sortable": queries.CONVERSION_SORTABLE,
        "state_table_sortable": queries.STATE_TABLE_SORTABLE,
        "stats_tables": STATS_TABLES,
        "unknown_ats": queries.UNKNOWN_ATS,
        "filter_fields": list(FILTER_FIELDS),
        "review_limit": REVIEW_LIMIT,
        "today": config.today(),
    }
