"""FastAPI + Jinja + HTMX dashboard.

Localhost only. Cross-machine and phone access is Tailscale to this port, never a
wider bind address. This is the whole interface: review and approve, tailor,
fill, and track. There is no second surface — the Telegram digest was removed in
favour of a pull queue on `/review`.
"""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, db, llm, prefilter, queries, resume, states, tailor
from ..normalize import UnparseableURL, detect_ats, normalize_apply_url

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


@app.exception_handler(HTTPException)
def htmx_aware_http_exception(request: Request, exc: HTTPException) -> Response:
    """Plain text for HTMX, FastAPI's JSON for everything else.

    HTMX does not swap on a 4xx, so before this every rejected action — an
    illegal transition, a stale id — did nothing at all on screen: the button
    simply went dead. The banner in base.html shows this text; sending it as
    JSON would put `{"detail": ...}` in front of me instead of the message.
    """
    if request.headers.get("HX-Request"):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


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


# The outcomes the entry form can record, and the only legal successors of
# `applied`. Anything else is a typed-in mistake, not a state.
KNOWN_OUTCOMES = frozenset({states.REJECTED, states.INTERVIEW, states.OFFER})

# Comp is stored as a plain SQLite integer. Above this, int() still succeeds and
# the driver raises OverflowError at bind time, which was a 500 on the form.
MAX_COMP = 100_000_000


def _parse_comp(raw: str, field: str) -> int | None:
    if not raw.strip():
        return None
    value = int(raw)  # ValueError here is caught and shown on the form
    if not 0 <= value <= MAX_COMP:
        raise ValueError(f"{field} should be between 0 and {MAX_COMP:,}, got {value:,}.")
    return value


@app.get("/applications/check-url", response_class=HTMLResponse)
def check_url(request: Request, conn: Conn, apply_url: Annotated[str, Query()] = "") -> Response:
    """Live dedup check on the entry form. Guards on `jobs.apply_url_norm`."""
    apply_url = apply_url.strip()
    if not apply_url:
        return HTMLResponse("")
    try:
        normalized = normalize_apply_url(apply_url)
    except UnparseableURL as exc:
        # Fires on `keyup`, so a URL is unparseable for most of the time it takes
        # to type one. Say so in the panel instead of throwing a traceback per key.
        return templates.TemplateResponse(
            request, "_url_check.html", {"unparseable": str(exc)}, status_code=200
        )
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

    # Checked before anything is written. This used to run after the application
    # was committed, so a value like "hired" raised InvalidTransition out of the
    # route: a 500 for the user, a saved row in the DB, and "Already tracked" on
    # the retry.
    if outcome and outcome not in KNOWN_OUTCOMES:
        return fail(
            f"{outcome!r} is not an outcome I can record. "
            f"Pick one of: {', '.join(sorted(KNOWN_OUTCOMES))}."
        )

    ats_type, ats_slug = detect_ats(apply_url)
    try:
        # Validated, not interpolated — an unparseable date used to reach the column.
        applied_ts = config.date_to_utc(applied_at) if applied_at else config.utcnow()
        comp_min_value = _parse_comp(comp_min, "Minimum comp")
        comp_max_value = _parse_comp(comp_max, "Maximum comp")
    except ValueError as exc:
        return fail(str(exc))

    if comp_min_value and comp_max_value and comp_min_value > comp_max_value:
        # Comp neither filters nor ranks — it exists to answer with. An inverted
        # range is a typo in a number I'd quote on a form, so catch it here.
        return fail(
            f"Minimum comp ({comp_min_value:,}) is above maximum ({comp_max_value:,})."
        )

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
                comp_min=comp_min_value,
                comp_max=comp_max_value,
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
            # Inside the transaction with everything else: an outcome that can't
            # be walked rolls the application back rather than leaving a half-
            # entered row the form then refuses to re-create.
            if outcome:
                path = (
                    [states.INTERVIEW, states.OFFER]
                    if outcome == states.OFFER
                    else [outcome]
                )
                for step in path:
                    states.transition(
                        conn, application_id, step, detail="manual entry: known outcome"
                    )
    except states.InvalidTransition as exc:
        return fail(str(exc))
    except sqlite3.IntegrityError as exc:
        existing = queries.find_job_by_url(conn, apply_url)
        if existing:
            return fail(
                f"Already tracked — {existing['title']} (job {existing['id']}). "
                "Update that application instead of logging a second one."
            )
        # A referral pointing at a contact that doesn't exist trips the foreign key.
        # Report it rather than letting the 500 surface.
        if "FOREIGN KEY" in str(exc).upper():
            return fail("That referral contact no longer exists. Pick another, or none.")
        return fail(f"The database rejected that: {exc}")
    except ValueError as exc:
        return fail(str(exc))

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
    except LookupError as exc:  # no such application — 404, not a crash
        raise HTTPException(404, str(exc)) from exc
    return _state_panel(request, conn, application_id)


@app.post("/applications/{application_id}/note", response_class=HTMLResponse)
def post_note(
    request: Request, conn: Conn, application_id: int, detail: Annotated[str, Form()]
) -> Response:
    # Checked first: log_event would otherwise insert straight into `events` and
    # surface the foreign-key violation as a 500 instead of a 404, which is what
    # post_transition already does correctly.
    if queries.get_application(conn, application_id) is None:
        raise HTTPException(404, "no such application")
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
    try:
        queries.set_would_apply_anyway(conn, application_id, int(would_apply_anyway))
    except ValueError as exc:  # the guard in queries raises on anything but 0/1
        raise HTTPException(422, str(exc)) from exc
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


# =================================== tailor ===================================
#
# The Phase 2 gate: paste a JD, get a tailored PDF plus a diff against master.
# `.claude/rules/tailoring.md` requires the diff before the resume is used, and
# requires that a diff which cannot be produced blocks the packet — so a
# validation failure renders the reason here instead of a PDF link.


@app.get("/tailor", response_class=HTMLResponse)
def tailor_form(request: Request, conn: Conn) -> Response:
    return templates.TemplateResponse(
        request,
        "tailor.html",
        {"page": "tailor", "corpus": _corpus_size(conn), "jd_text": "", "result": None,
         "error": None},
    )


@app.post("/tailor", response_class=HTMLResponse)
def post_tailor(
    request: Request,
    conn: Conn,
    jd_text: Annotated[str, Form()],
    limit: Annotated[int, Form()] = 10,
) -> Response:
    context: dict[str, Any] = {
        "page": "tailor",
        "corpus": _corpus_size(conn),
        "jd_text": jd_text,
        "result": None,
        "error": None,
    }
    if not jd_text.strip():
        context["error"] = "Paste a job description first."
        return templates.TemplateResponse(request, "tailor.html", context, status_code=422)

    try:
        result = tailor.tailor(
            conn,
            jd_text,
            limit=max(1, min(limit, 30)),
            # One JD pasted by hand. Nothing follows inside the 5-minute cache
            # TTL, so writing an entry here is a 125% charge against a read that
            # never happens. Phase 3's queue worker is the caller that wants it.
            expect_repeat=False,
        )
    except tailor.FabricationError as exc:
        # Not an error page: this is the guarantee working, and seeing exactly
        # what was rejected is the point.
        context["error"] = f"Rejected — the model asserted something the corpus does not support.\n\n{exc}"
        return templates.TemplateResponse(request, "tailor.html", context, status_code=422)
    # Named causes only. This was `(tailor.TailorError, Exception)`, which is
    # just `Exception` — every bug in the request path rendered as a tidy 422
    # telling me the tailoring failed, when the truth was a TypeError I wanted
    # to see. Anything unlisted now propagates and 500s.
    except (tailor.TailorError, llm.LLMError) as exc:
        context["error"] = f"{type(exc).__name__}: {exc}"
        return templates.TemplateResponse(request, "tailor.html", context, status_code=422)

    # Second precision collided: two tailors inside the same second wrote the
    # same path, and the second silently overwrote the first. `out/` is
    # disposable, but not while I still have the tab open.
    stamp = config.utcnow().replace(":", "").replace("-", "")
    # No company name in the filename — see CLAUDE.local.md, Discretion.
    pdf_name = f"tailored_{stamp}_{secrets.token_hex(3)}.pdf"
    try:
        resume.render(
            resume.build_document(conn, selection=result.selection()),
            config.OUT_DIR / pdf_name,
        )
    except resume.ResumeError as exc:
        context["error"] = f"Tailoring passed but the PDF did not render: {exc}"
        return templates.TemplateResponse(request, "tailor.html", context, status_code=422)

    context["result"] = {
        "reasoning": result.reasoning,
        "diff": tailor.diff(result),
        "pdf": pdf_name,
        "kept": len(result.bullets),
        "reworded": sum(1 for b in result.bullets if b.changed),
    }
    return templates.TemplateResponse(request, "tailor.html", context)


@app.get("/out/{filename}")
def download_render(filename: str) -> Response:
    """Serve a rendered PDF. `out/` is disposable; the DB holds what was sent."""
    if "/" in filename or "\\" in filename or not filename.endswith(".pdf"):
        raise HTTPException(404, "no such file")
    path = (config.OUT_DIR / filename).resolve()
    if not path.is_file() or config.OUT_DIR.resolve() not in path.parents:
        raise HTTPException(404, "no such file")
    return FileResponse(path, media_type="application/pdf", filename=filename)


def _corpus_size(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "bullets": conn.execute("SELECT COUNT(*) AS n FROM bullets").fetchone()["n"],
        "experiences": conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"],
        "projects": conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"],
    }


# =================================== review ===================================
#
# The curated queue, and the only place a job is approved. This replaced the
# Telegram digest: same selection, same ordering, one surface instead of two.
#
# Dropping the push notification also dropped a whole class of guard. A digest
# had to refuse a second send, because being messaged twice put sixteen postings
# in front of me on a morning planned for eight. A pull queue cannot do that —
# I look when I am ready to work and stop when I am done — so `digest_sent`,
# the once-per-day check, and the long-polling callback handler all went with it.

REVIEW_LIMIT = 8
JD_EXCERPT = 1200


def _review_batch(conn: sqlite3.Connection, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Top `limit` scored postings, plus how many are waiting in total."""
    rows = conn.execute(
        """
        SELECT a.id AS application_id, a.score, a.score_reasoning,
               j.title, j.location, j.remote, j.apply_url, j.jd_text,
               j.comp_min, j.comp_max, c.name AS company,
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

    cfg = prefilter.load()
    ranked = sorted(
        rows, key=lambda r: (prefilter.location_tier(r, cfg), -(r["score"] or 0))
    )

    batch = []
    for row in ranked[:limit]:
        where = row["location"] or "—"
        if (row["remote"] or "").lower() == "remote":
            where = f"Remote · {where}" if row["location"] else "Remote"
        comp = None
        if row["comp_min"] and row["comp_max"]:
            comp = f"${row['comp_min'] // 1000}k–${row['comp_max'] // 1000}k"
        elif row["comp_min"]:
            comp = f"from ${row['comp_min'] // 1000}k"
        elif row["comp_max"]:
            comp = f"up to ${row['comp_max'] // 1000}k"
        jd = (row["jd_text"] or "").strip()
        batch.append({
            "application_id": int(row["application_id"]),
            "title": row["title"], "company": row["company"], "where": where,
            "score": row["score"] or 0, "reason": row["score_reasoning"] or "",
            "apply_url": row["apply_url"], "comp": comp, "referral": row["referral"],
            "excerpt": (jd[:JD_EXCERPT] + "…") if len(jd) > JD_EXCERPT else jd,
        })
    return batch, len(rows)


@app.get("/review", response_class=HTMLResponse)
def review(
    request: Request, conn: Conn, limit: Annotated[int, Query()] = REVIEW_LIMIT
) -> Response:
    limit = max(1, min(limit, 50))
    batch, waiting = _review_batch(conn, limit)
    return templates.TemplateResponse(
        request, "review.html",
        {"page": "review", "batch": batch, "waiting": waiting, "limit": limit},
    )


def _decide(request: Request, conn: sqlite3.Connection, application_id: int, to_state: str) -> Response:
    row = queries.get_application(conn, application_id)
    if row is None:
        raise HTTPException(404, "no such application")
    try:
        states.transition(conn, application_id, to_state, detail="review queue")
    except states.InvalidTransition as exc:
        # Already decided elsewhere — two tabs, or a double-click. Say what it is
        # rather than 500ing, and leave the card showing the real outcome.
        raise HTTPException(409, str(exc)) from exc
    return templates.TemplateResponse(
        request, "_review_done.html",
        {
            "application_id": application_id,
            "title": row["title"],
            "outcome": "approved" if to_state == states.JOB_APPROVED else "skipped",
        },
    )


@app.post("/review/{application_id}/approve", response_class=HTMLResponse)
def review_approve(request: Request, conn: Conn, application_id: int) -> Response:
    return _decide(request, conn, application_id, states.JOB_APPROVED)


@app.post("/review/{application_id}/skip", response_class=HTMLResponse)
def review_skip(request: Request, conn: Conn, application_id: int) -> Response:
    return _decide(request, conn, application_id, states.SKIPPED)


# ================================= fill helper =================================
#
# The pain this exists for: an ATS makes you retype every role into separate
# Company / Title / Start / End / Description fields, and the same information
# is already on the resume it just accepted. That is most of the ten minutes.
#
# This is not autofill and deliberately not a browser extension — see
# docs/architecture.md, "Why no browser automation". It is the same data laid out
# one field per box with a copy button, which needs no per-ATS adapter and cannot
# break when Workday ships a change. The content is identical for every
# application, so it does not depend on a packet and works today.

_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")


def _date_forms(month: str | None) -> dict[str, str] | None:
    """`YYYY-MM` in every shape an ATS asks for, because they disagree.

    Workday wants a month name or number from a dropdown and the year separately,
    Greenhouse takes `MM/YYYY`, a few want the ISO month. Rendering all of them
    beats retyping or doing the conversion in my head at 11pm.
    """
    if not month:
        return None
    text = str(month).strip()
    parts = text.split("-")
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


@app.get("/fill", response_class=HTMLResponse)
def fill_helper(request: Request, conn: Conn) -> Response:
    facts = queries.profile_facts(conn)
    identity = [
        (label, facts.get(key))
        for label, key in (
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
        if facts.get(key)
    ]

    bullets = queries.corpus_bullets(conn)
    by_experience: dict[int, list[sqlite3.Row]] = {}
    by_project: dict[int, list[sqlite3.Row]] = {}
    for row in bullets:
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
            "start": _date_forms(row["start_month"]), "end": _date_forms(row["end_month"]),
            "bullets": items, "description": block,
        })

    projects = []
    for row in queries.corpus_projects(conn):
        items, block = _described(by_project.get(int(row["id"]), []))
        projects.append({
            "name": row["name"], "url": row["url"], "role": row["role"],
            "start": _date_forms(row["start_month"]), "end": _date_forms(row["end_month"]),
            "bullets": items, "description": block,
        })

    education = [
        {"school": row["school"], "degree": row["degree"], "field": row["field"],
         "start": _date_forms(row["start_month"]), "end": _date_forms(row["end_month"])}
        for row in queries.corpus_education(conn)
    ]

    return templates.TemplateResponse(
        request, "fill.html",
        {"page": "fill", "identity": identity, "experiences": experiences,
         "projects": projects, "education": education},
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
            "spend": queries.llm_spend(conn),
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
