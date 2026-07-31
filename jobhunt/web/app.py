"""FastAPI + Jinja + HTMX dashboard.

Localhost only. Cross-machine and phone access is Tailscale to this port, never a
wider bind address. Telegram handles approvals and notifications; the dashboard
handles filling and tracking.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, db, queries, states
from ..normalize import detect_ats, normalize_apply_url

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly at startup rather than on the first query.
    conn = db.connect()
    try:
        db.verify_schema(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="jobhunt", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def get_conn():
    """One connection per request. The mini's process is the only writer."""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


templates.env.filters["pct"] = _pct
templates.env.globals["PIPELINE_ORDER"] = states.PIPELINE_ORDER
# `offer` is both terminal and the end of the pipeline; only show it once.
templates.env.globals["DEAD_ENDS"] = sorted(
    states.TERMINAL.difference(states.PIPELINE_ORDER)
)


# ================================== pipeline ==================================


FILTER_FIELDS = ("q", "state", "ats", "source", "referral", "waa")
DEFAULT_SORT = "applied"
DEFAULT_DIRECTION = "desc"
# Columns whose most useful first click is newest/highest first.
DESCENDING_FIRST = frozenset({"applied", "waa", "state"})


def _table_view(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
    """Filter/sort state read from the query string, plus everything to render it."""
    params = request.query_params
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
        headers.append(
            {
                "column": column,
                "label": label,
                "active": is_active,
                "direction": direction if is_active else None,
                "query": urlencode({**active, "sort": column, "dir": next_direction}),
            }
        )

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
        for step in list(states.PIPELINE_ORDER)
        + sorted(states.TERMINAL.difference(states.PIPELINE_ORDER))
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn: Conn) -> Response:
    view = _table_view(request, conn)
    # Filter and sort controls swap the table alone; a plain visit or a reload of
    # the pushed URL renders the whole page from the same query string.
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "_application_table.html", {**view, "oob": True}
        )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "page": "pipeline",
            "counts": queries.state_counts(conn),
            "health": queries.queue_health(conn),
            "honesty": queries.honesty(conn),
            **view,
        },
    )


@app.get("/applications/new", response_class=HTMLResponse)
def new_application_form(request: Request, conn: Conn) -> Response:
    return templates.TemplateResponse(
        request,
        "new.html",
        {
            "page": "new",
            "contacts": queries.list_contacts(conn),
            "today": config.today(),
            "form": {},
            "error": None,
        },
    )


@app.get("/applications/check-url", response_class=HTMLResponse)
def check_url(request: Request, conn: Conn, apply_url: Annotated[str, Query()] = "") -> Response:
    """Live dedup check on the entry form. Guards on `jobs.apply_url_norm`."""
    apply_url = apply_url.strip()
    if not apply_url:
        return HTMLResponse("")
    normalized = normalize_apply_url(apply_url)
    ats_type, ats_slug = detect_ats(apply_url)
    existing = queries.find_job_by_url(conn, apply_url)
    application = None
    if existing:
        application = conn.execute(
            "SELECT id, state FROM applications WHERE job_id = ?", (existing["id"],)
        ).fetchone()
    return templates.TemplateResponse(
        request,
        "_url_check.html",
        {
            "normalized": normalized,
            "ats_type": ats_type,
            "ats_slug": ats_slug,
            "existing": existing,
            "application": application,
        },
    )


@app.post("/applications")
def create_application(
    request: Request,
    conn: Conn,
    company: Annotated[str, Form()],
    title: Annotated[str, Form()],
    apply_url: Annotated[str, Form()],
    would_apply_anyway: Annotated[int, Form()],
    applied_at: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    remote: Annotated[str, Form()] = "unknown",
    source: Annotated[str, Form()] = "manual",
    comp_min: Annotated[str, Form()] = "",
    comp_max: Annotated[str, Form()] = "",
    posted_at: Annotated[str, Form()] = "",
    referral_contact_id: Annotated[str, Form()] = "",
    outcome: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    jd_text: Annotated[str, Form()] = "",
) -> Response:
    """Log an application I submitted by hand.

    Seeds straight into `applied` — see the (None, APPLIED) note in states.py.
    """
    submitted = {
        "company": company,
        "title": title,
        "apply_url": apply_url,
        "location": location,
        "remote": remote,
        "source": source,
        "comp_min": comp_min,
        "comp_max": comp_max,
        "posted_at": posted_at,
        "applied_at": applied_at,
        "referral_contact_id": referral_contact_id,
        "outcome": outcome,
        "note": note,
        "jd_text": jd_text,
        "would_apply_anyway": would_apply_anyway,
    }

    def fail(message: str) -> Response:
        return templates.TemplateResponse(
            request,
            "new.html",
            {
                "page": "new",
                "contacts": queries.list_contacts(conn),
                "today": config.today(),
                "form": submitted,
                "error": message,
            },
            status_code=422,
        )

    if not company.strip() or not title.strip() or not apply_url.strip():
        return fail("Company, title, and apply URL are all required.")

    ats_type, ats_slug = detect_ats(apply_url)
    applied_ts = f"{applied_at}T12:00:00Z" if applied_at else config.utcnow()

    try:
        with db.transaction(conn):  # company + job + application + event, or nothing
            company_id = queries.upsert_company(
                conn, company, ats_type=ats_type, ats_slug=ats_slug
            )
            job_id = queries.insert_job(
                conn,
                company_id=company_id,
                title=title,
                apply_url=apply_url,
                source=source.strip() or "manual",
                location=location.strip() or None,
                remote=remote,
                jd_text=jd_text.strip() or None,
                comp_min=int(comp_min) if comp_min.strip() else None,
                comp_max=int(comp_max) if comp_max.strip() else None,
                posted_at=posted_at or None,
            )
            application_id = states.create(
                conn,
                job_id=job_id,
                state=states.APPLIED,
                detail="manual entry",
                applied_at=applied_ts,
                would_apply_anyway=int(would_apply_anyway),
                referral_contact_id=(
                    int(referral_contact_id) if referral_contact_id.strip() else None
                ),
            )
            if note.strip():
                states.log_event(conn, application_id, "note", detail=note.strip())
    except sqlite3.IntegrityError:
        existing = queries.find_job_by_url(conn, apply_url)
        if existing:
            return fail(
                f"Already tracked — {existing['title']} (job {existing['id']}). "
                "Update that application instead of logging a second one."
            )
        raise
    except ValueError as exc:
        return fail(str(exc))

    # Outcome already known at entry time: real transitions, not a backdated state.
    if outcome:
        path = (
            [states.INTERVIEW, states.OFFER] if outcome == states.OFFER else [outcome]
        )
        for step in path:
            states.transition(
                conn, application_id, step, detail="manual entry: known outcome"
            )

    return RedirectResponse(f"/applications/{application_id}", status_code=303)


@app.get("/applications/{application_id}", response_class=HTMLResponse)
def application_detail(request: Request, conn: Conn, application_id: int) -> Response:
    application = queries.get_application(conn, application_id)
    if application is None:
        raise HTTPException(404, "no such application")
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "page": "detail",
            "app": application,
            "events": queries.events_for(conn, application_id),
            "referrals": queries.contacts_at_company(conn, application["company_id"]),
        },
    )


@app.post("/applications/{application_id}/transition", response_class=HTMLResponse)
def post_transition(
    request: Request,
    conn: Conn,
    application_id: int,
    to_state: Annotated[str, Form()],
    detail: Annotated[str, Form()] = "",
) -> Response:
    try:
        states.transition(
            conn, application_id, to_state, detail=detail.strip() or "dashboard"
        )
    except states.InvalidTransition as exc:
        raise HTTPException(422, str(exc)) from exc
    return _state_panel(request, conn, application_id)


@app.post("/applications/{application_id}/note", response_class=HTMLResponse)
def post_note(
    request: Request, conn: Conn, application_id: int, detail: Annotated[str, Form()]
) -> Response:
    if detail.strip():
        states.log_event(conn, application_id, "note", detail=detail.strip())
    return _state_panel(request, conn, application_id)


@app.post("/applications/{application_id}/honesty", response_class=HTMLResponse)
def post_honesty(
    request: Request,
    conn: Conn,
    application_id: int,
    would_apply_anyway: Annotated[int, Form()],
) -> Response:
    queries.set_would_apply_anyway(conn, application_id, int(would_apply_anyway))
    return _state_panel(request, conn, application_id)


def _state_panel(request: Request, conn: sqlite3.Connection, application_id: int) -> Response:
    """The HTMX swap target shared by every action on the detail page."""
    application = queries.get_application(conn, application_id)
    if application is None:
        raise HTTPException(404, "no such application")
    return templates.TemplateResponse(
        request,
        "_state_panel.html",
        {"app": application, "events": queries.events_for(conn, application_id)},
    )


# =================================== stats ===================================


# Conversion tables on the stats page: URL prefix -> the bucket column's header.
STATS_TABLES = {"ats": "ATS", "source": "Source", "referral": "Referral"}
# Each table sorts independently, so its parameters are namespaced by prefix.
STATS_DEFAULTS = {"ats": ("applied", "desc"), "source": ("applied", "desc"),
                  "referral": ("applied", "desc"), "states": ("state", "asc")}


def _stats_headers(
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
        query = {**every_param, f"{prefix}_sort": column, f"{prefix}_dir": next_direction}
        headers.append(
            {
                "column": column,
                "label": label,
                "active": is_active,
                "direction": direction if is_active else None,
                "numeric": index >= numeric_from,
                "query": urlencode(query),
            }
        )
    return headers


def _stats_view(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
    params = request.query_params
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
    every_param = {}
    for prefix, (sort, direction) in ordering.items():
        every_param[f"{prefix}_sort"] = sort
        every_param[f"{prefix}_dir"] = direction

    tables = {}
    for prefix, bucket_label in STATS_TABLES.items():
        sort, direction = ordering[prefix]
        columns = {**queries.CONVERSION_SORTABLE, "label": bucket_label}
        tables[prefix] = {
            "prefix": prefix,
            "rows": queries.sort_buckets(
                queries.conversion_by(conn, prefix), sort, direction
            ),
            "sort": sort,
            "direction": direction,
            "headers": _stats_headers(prefix, columns, sort, direction, every_param),
        }

    sort, direction = ordering["states"]
    tables["states"] = {
        "prefix": "states",
        "rows": queries.state_rows(conn, sort, direction),
        "sort": sort,
        "direction": direction,
        "headers": _stats_headers(
            "states", queries.STATE_TABLE_SORTABLE, sort, direction, every_param
        ),
    }
    return {"tables": tables}


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, conn: Conn) -> Response:
    view = _stats_view(request, conn)
    requested = request.query_params.get("table")
    # A sort click swaps its own table; the other three keep their state.
    if request.headers.get("HX-Request") and requested in view["tables"]:
        template = (
            "_stats_states.html" if requested == "states" else "_stats_conversion.html"
        )
        return templates.TemplateResponse(
            request, template, {"table": view["tables"][requested]}
        )
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "page": "stats",
            "overall": queries.overall(conn),
            "honesty": queries.honesty(conn),
            "health": queries.queue_health(conn),
            **view,
        },
    )


@app.post("/theme")
def set_theme(
    mode: Annotated[str, Form()], next: Annotated[str, Form()] = "/"
) -> Response:
    """Persist the color theme in a cookie. `system` clears it and follows the OS."""
    if mode not in ("system", "light", "dark"):
        raise HTTPException(422, "theme is system, light, or dark")
    # Only same-site relative paths, so the redirect can't be pointed elsewhere.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    if mode == "system":
        response.delete_cookie("theme", path="/")
    else:
        response.set_cookie(
            "theme", mode, max_age=60 * 60 * 24 * 365, path="/", samesite="lax"
        )
    return response


def run_dev() -> None:
    """`make dev`. Refuses to bind anywhere but loopback."""
    import ipaddress

    import uvicorn

    address = ipaddress.ip_address(config.HOST)
    if not address.is_loopback:
        raise RuntimeError(
            f"refusing to bind {config.HOST}: the dashboard is loopback only. "
            "Reach it from other machines over Tailscale."
        )
    uvicorn.run("jobhunt.web.app:app", host=config.HOST, port=config.PORT, reload=True)


if __name__ == "__main__":
    run_dev()
