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


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    conn: Conn,
    state: Annotated[str | None, Query()] = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "page": "pipeline",
            "counts": queries.state_counts(conn),
            "applications": queries.list_applications(conn, state=state),
            "filter_state": state,
            "health": queries.queue_health(conn),
            "honesty": queries.honesty(conn),
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


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, conn: Conn) -> Response:
    context: dict[str, Any] = {
        "page": "stats",
        "counts": queries.state_counts(conn),
        "by_ats": queries.conversion_by(conn, "ats"),
        "by_source": queries.conversion_by(conn, "source"),
        "by_referral": queries.conversion_by(conn, "referral"),
        "honesty": queries.honesty(conn),
        "health": queries.queue_health(conn),
    }
    return templates.TemplateResponse(request, "stats.html", context)


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
