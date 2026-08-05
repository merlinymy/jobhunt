"""FastAPI + Jinja + HTMX dashboard.

Localhost only. Cross-machine and phone access is Tailscale Serve in front of
this port, never a wider bind address — `serve()` refuses anything that is not
loopback. This is the whole interface: review and approve, tailor, fill, and
track. There is no second surface — the Telegram digest was removed in favour of
a pull queue on `/review`.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
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

from .. import answers, config, db, llm, queries, resume, states, tailor
from ..normalize import UnparseableURL, detect_ats, normalize_apply_url
from . import api, views

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

# Registered before any catch-all. Starlette matches in declaration order, so
# ordering is the real guard and the prefix checks downstream are the backstop.
app.include_router(api.router)


@app.middleware("http")
async def same_site_only(request: Request, call_next):
    """Reject cross-site writes.

    There is no auth and no CSRF token here, and the loopback bind was doing all
    the work: any page open in this machine's browser could POST to
    127.0.0.1:8000 and approve, skip, or transition something. Serve keeps the
    port off the wider network, but not away from a local browser tab.

    `None` is allowed through so curl, the launchd smoke checks, and anything
    without fetch metadata keep working — this raises the floor, it is not a
    boundary to rely on.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "same-site", "none"):
            return PlainTextResponse("cross-site request refused", status_code=403)
    return await call_next(request)


@app.exception_handler(HTTPException)
def http_exception(request: Request, exc: HTTPException) -> Response:
    """One error shape per client. Plain text for HTMX, `{"error": ...}` for the API.

    HTMX does not swap on a 4xx, so before this every rejected action — an
    illegal transition, a stale id — did nothing at all on screen: the button
    simply went dead. The banner in base.html shows this text; sending it as
    JSON would put `{"detail": ...}` on screen instead of the message.

    The API gets `error`, not FastAPI's `detail`, so the client has exactly one
    field to read on every failure regardless of which layer raised it.
    """
    if request.headers.get("HX-Request"):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers
        )
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


@app.exception_handler(RequestValidationError)
def validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Pydantic's list of field errors, flattened to the one sentence to show."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error.get("loc", ()) if p != "body")
        parts.append(f"{location}: {error.get('msg')}" if location else str(error.get("msg")))
    message = "; ".join(parts) or "the request body was not valid"
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": message}, status_code=422)
    return PlainTextResponse(message, status_code=422)


@app.exception_handler(db.DatabaseLocationError)
def database_unavailable(request: Request, exc: db.DatabaseLocationError) -> Response:
    """503 with the reason, rather than a 500 and a traceback in the log.

    The disk can go away under a running dashboard — a knocked cable is the
    likely one — and every request then fails inside `get_conn`. Being told
    "the external disk is not mounted" from a phone is the difference between
    a thirty-second fix and assuming the app is broken.
    """
    return PlainTextResponse(str(exc).split("\n")[0], status_code=503)


def get_conn():
    """One connection per request. The mini's process is the only writer."""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


templates.env.filters["pct"] = views.pct
templates.env.globals["PIPELINE_ORDER"] = states.PIPELINE_ORDER
templates.env.globals["DEAD_ENDS"] = views.dead_ends()


# ================================== pipeline ==================================


@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn: Conn) -> Response:
    view = views.table_view(conn, request.query_params)
    # Filter and sort controls swap the table alone; a plain visit or a reload of
    # the pushed URL renders the whole page from the same query string.
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "_application_table.html", {**view, "oob": True}
        )
    return templates.TemplateResponse(
        request,
        "index.html",
        {"page": "pipeline", **views.pipeline_view(conn, request.query_params)},
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
        {"page": "tailor", "corpus": queries.corpus_counts(conn), "jd_text": "", "result": None,
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
        "corpus": queries.corpus_counts(conn),
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
            resume.build_document(conn, selection=result.selection(), jd_text=jd_text),
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

@app.get("/review", response_class=HTMLResponse)
def review(
    request: Request, conn: Conn, limit: Annotated[int, Query()] = views.REVIEW_LIMIT
) -> Response:
    limit = max(1, min(limit, 50))
    batch, waiting = views.review_batch(conn, limit)
    return templates.TemplateResponse(
        request, "review.html",
        {"page": "review", "batch": batch, "waiting": waiting, "limit": limit},
    )


def _decide(request: Request, conn: sqlite3.Connection, application_id: int, to_state: str) -> Response:
    row = queries.get_application(conn, application_id)
    if row is None:
        raise HTTPException(404, "no such application")
    siblings = queries.sibling_application_ids(conn, application_id)
    try:
        states.transition(conn, application_id, to_state, detail="review queue")
    except states.InvalidTransition as exc:
        # Already decided elsewhere — two tabs, or a double-click. Say what it is
        # rather than 500ing, and leave the card showing the real outcome.
        raise HTTPException(409, str(exc)) from exc

    # The same role in eight other cities is the same shot. Leaving them
    # `scored` means they surface again tomorrow and the queue never drains;
    # deciding them here is recorded as its own event naming the row that
    # decided them, so nothing vanishes without a trace.
    closed = 0
    for sibling in siblings:
        try:
            states.transition(
                conn, sibling, states.SKIPPED,
                detail=f"duplicate listing of application {application_id}",
            )
            closed += 1
        except states.InvalidTransition:
            continue

    return templates.TemplateResponse(
        request, "_review_done.html",
        {
            "application_id": application_id,
            "title": row["title"],
            "siblings": closed,
            "outcome": "approved" if to_state == states.JOB_APPROVED else "skipped",
        },
    )


@app.post("/review/{application_id}/approve", response_class=HTMLResponse)
def review_approve(request: Request, conn: Conn, application_id: int) -> Response:
    return _decide(request, conn, application_id, states.JOB_APPROVED)


@app.post("/review/{application_id}/skip", response_class=HTMLResponse)
def review_skip(request: Request, conn: Conn, application_id: int) -> Response:
    return _decide(request, conn, application_id, states.SKIPPED)


# =================================== packet ===================================
#
# What `/review` approve leads to. `tailor.build_packet` does the work; this is
# the view over it, plus the button that triggers a build on demand.
#
# On demand *and* batched: `make tailor` builds every `job_approved` row for the
# scheduled path, and this button exists because approving something and waiting
# for a launchd timer is not how anyone actually applies to a job.


def _packet(conn: sqlite3.Connection, application_id: int) -> dict:
    """`views.packet_view` plus the page marker, translating its LookupError."""
    try:
        return {"page": "packet", **views.packet_view(conn, application_id)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/packet/{application_id}", response_class=HTMLResponse)
def packet(request: Request, conn: Conn, application_id: int) -> Response:
    context = _packet(conn, application_id)
    extra = views.answers_view(conn, application_id)
    # Record the gaps on every view, not only on build: the count is what tells
    # me a question belongs in facts.yaml, and I see the packet more often than
    # I build one.
    answers.flag_unknowns(conn, extra["answers"], application_id)
    return templates.TemplateResponse(
        request, "packet.html",
        {**context, "answers": extra["answers"], "unknowns": extra["unknowns"]},
    )


@app.post("/packet/{application_id}/build", response_class=HTMLResponse)
def packet_build(request: Request, conn: Conn, application_id: int) -> Response:
    """Tailor, render, store, advance. Synchronous — it is one model call."""
    error = None
    try:
        tailor.build_packet(conn, application_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except tailor.NoJobDescription as exc:
        error = str(exc)
    except tailor.FabricationError as exc:
        # The guarantee working, not a crash. Nothing was rendered and the row
        # stays `job_approved`, so it can be retried or done by hand.
        error = f"Rejected — the model asserted something the corpus does not support.\n\n{exc}"
    except (tailor.TailorError, llm.LLMError, resume.ResumeError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    context = _packet(conn, application_id)
    context["error"] = error
    return templates.TemplateResponse(request, "_packet_body.html", context)


@app.post("/packet/{application_id}/answers/{key}/generate", response_class=HTMLResponse)
def packet_generate_answer(
    request: Request, conn: Conn, application_id: int, key: str
) -> Response:
    """Draft one narrative answer and cache it against the company."""
    question = answers.BY_KEY.get(key)
    if question is None:
        raise HTTPException(404, f"no question {key!r}")
    row = conn.execute(
        """
        SELECT j.company_id, j.title, j.jd_text, c.name AS company
          FROM applications a JOIN jobs j ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.id = ?
        """,
        (application_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "no such application")
    try:
        answers.generate(
            conn, question, company=row["company"], title=row["title"],
            jd_text=row["jd_text"], company_id=int(row["company_id"]),
            application_id=application_id,
        )
    except (answers.AnswerError, llm.LLMError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return templates.TemplateResponse(
        request, "_packet_answers.html", views.answers_view(conn, application_id)
    )


@app.post("/packet/{application_id}/answers/{key}/set", response_class=HTMLResponse)
def packet_set_answer(
    request: Request, conn: Conn, application_id: int, key: str,
    answer: Annotated[str, Form()],
) -> Response:
    """Store a per-company fact I typed — salary, and nothing else so far.

    Fact tier and `source = 'user'`, so it is returned verbatim to every later
    application to this employer. That is the point: quoting one company two
    different numbers is the failure this prevents.
    """
    question = answers.BY_KEY.get(key)
    if question is None:
        raise HTTPException(404, f"no question {key!r}")
    if question.tier != answers.FACT:
        raise HTTPException(422, f"{key!r} is narrative-tier; draft it instead")
    if not answer.strip():
        raise HTTPException(422, "an empty answer is not an answer")
    context = views.answers_view(conn, application_id)
    answers.put(conn, question.key, question.text, answer.strip(),
                tier=answers.FACT, source="user", company_id=context["company_id"])
    return templates.TemplateResponse(
        request, "_packet_answers.html", views.answers_view(conn, application_id)
    )


@app.get("/packet/{application_id}/resume.pdf")
def packet_resume(conn: Conn, application_id: int) -> Response:
    """The stored bytes, not a re-render.

    `.claude/rules/data-layer.md`: `resume_pdf` is the record of what was
    actually sent. Serving a fresh render here would quietly make that false the
    first time a template changed.
    """
    row = conn.execute(
        "SELECT resume_pdf FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None or row["resume_pdf"] is None:
        raise HTTPException(404, "no packet built for this application")
    return Response(
        content=row["resume_pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="resume_{application_id}.pdf"'},
    )


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

@app.get("/fill", response_class=HTMLResponse)
def fill_helper(request: Request, conn: Conn) -> Response:
    return templates.TemplateResponse(
        request, "fill.html", {"page": "fill", **views.fill_view(conn)}
    )


# =================================== stats ===================================


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, conn: Conn) -> Response:
    view = views.stats_tables(conn, request.query_params)
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


def _assert_loopback(host: str) -> None:
    """Refuse to bind anywhere but loopback.

    This guard is what makes Tailscale Serve safe: Serve terminates TLS on the
    tailnet and dials 127.0.0.1 from this machine, so the port being unreachable
    any other way is the entire access control story. There is no auth here.

    Resolves hostnames rather than only accepting literals — the old version
    called `ipaddress.ip_address` directly, so `JOBHUNT_HOST=localhost` died with
    a bare ValueError instead of working.
    """
    import ipaddress
    import socket

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise RuntimeError(f"JOBHUNT_HOST={host!r} does not resolve") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or not all(address.is_loopback for address in addresses):
        raise RuntimeError(
            f"refusing to bind {host!r}: the dashboard is loopback only. Reach it "
            "from other machines over Tailscale Serve, which proxies to this port."
        )


def serve(*, reload: bool = False, wait_for_db: float = 0.0) -> None:
    """Run the dashboard. Production by default; `--reload` is the opt-in.

    `reload` used to be hardcoded on, and the launchd agent ran this same
    function — so the always-on dashboard was running uvicorn's dev auto-reloader
    with `watchfiles` not installed, meaning a stat-poll of the whole repo every
    quarter second, two processes, and a restart mid-request on any `git pull`.
    The default is now the safe one: forgetting the flag gets you production.
    """
    import uvicorn

    from .. import doctor
    from . import logconf

    _assert_loopback(config.HOST)
    if wait_for_db:
        # KeepAlive is a plain true, so without this an unmounted disk at login
        # means a traceback every ThrottleInterval forever. One sleeper instead,
        # which comes back on its own the moment the volume appears.
        doctor.wait_for_db(wait_for_db)
    uvicorn.run(
        "jobhunt.web.app:app",
        host=config.HOST,
        port=config.PORT,
        reload=reload,
        # The reloader wants the console; a daemon wants its own rotating file.
        log_config=None if reload else logconf.dict_config(),
        # Tailscale Serve sets X-Forwarded-*; trust it only from the local proxy.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        timeout_graceful_shutdown=10,
        server_header=False,
    )


def run_dev() -> None:
    """`make dev`. Auto-reload; never what the launchd agent runs."""
    serve(reload=True)


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="run the jobhunt dashboard")
    parser.add_argument("--reload", action="store_true", help="dev auto-reload")
    parser.add_argument(
        "--wait-for-db", type=float, default=0.0, metavar="SECONDS",
        help="wait for the database volume before binding (launchd uses this)",
    )
    args = parser.parse_args(argv)
    serve(reload=args.reload, wait_for_db=args.wait_for_db)


if __name__ == "__main__":
    main()
