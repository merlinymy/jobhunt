"""The JSON API the React client talks to.

Routing and serialization only. Everything that decides what a page shows is in
`views.py`, everything that writes is in `actions.py`, and both are shared with
the Jinja templates while those still exist.

Responses are plain dicts, not Pydantic models. A response model per endpoint
would be a second schema to keep in step with hand-written SQL that changes shape
per route, for one consumer whose types are checked by `tsc`. Requests *are*
Pydantic: `POST /api/applications` has fourteen fields that were being coerced by
hand, and a structured 422 beats re-deriving that in TypeScript.

Errors are uniform — `{"error": "<sentence>"}` — so the client needs exactly one
parse path. Status codes carry the meaning: 404 gone, 409 already decided by
someone else, 422 rejected.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .. import answers, config, db, queries
from ..normalize import UnparseableURL, detect_ats, normalize_apply_url
from . import actions, views

router = APIRouter(prefix="/api")


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _params(request: Request) -> dict[str, str]:
    return dict(request.query_params)


def _found(value: Any, message: str = "no such application") -> Any:
    if value is None:
        raise HTTPException(404, message)
    return value


# ------------------------------------------------------------------- models ---


class ApplicationCreate(BaseModel):
    company: str
    title: str
    apply_url: str
    would_apply_anyway: int = Field(0, ge=0, le=1)
    applied_at: str = ""
    location: str = ""
    remote: str = "unknown"
    source: str = "manual"
    comp_min: str | int | None = None
    comp_max: str | int | None = None
    posted_at: str = ""
    referral_contact_id: str | int | None = None
    outcome: str = ""
    note: str = ""
    jd_text: str = ""


class TransitionRequest(BaseModel):
    to_state: str
    detail: str = ""


class NoteRequest(BaseModel):
    detail: str


class HonestyRequest(BaseModel):
    would_apply_anyway: int = Field(ge=0, le=1)


class AnswerRequest(BaseModel):
    answer: str


class TailorRequest(BaseModel):
    jd_text: str
    limit: int | None = None


# --------------------------------------------------------------------- meta ---


@router.get("/meta")
def meta() -> dict[str, Any]:
    """State vocabulary and column definitions, so the client hardcodes none of it."""
    return views.meta()


# ----------------------------------------------------------------- pipeline ---


@router.get("/pipeline")
def pipeline(request: Request, conn: Conn) -> dict[str, Any]:
    return views.pipeline_view(conn, _params(request))


@router.get("/applications")
def list_applications(request: Request, conn: Conn) -> dict[str, Any]:
    view = views.table_view(conn, _params(request))
    return {
        "applications": view["applications"],
        "total": len(view["applications"]),
        "headers": view["headers"],
        "filters": view["filters"],
        "facets": view["facets"],
        "sort": view["sort"],
        "direction": view["direction"],
    }


@router.get("/applications/check-url")
def check_url(conn: Conn, apply_url: Annotated[str, Query()] = "") -> dict[str, Any]:
    """Live dedup check on the entry form. Guards on `jobs.apply_url_norm`."""
    apply_url = apply_url.strip()
    if not apply_url:
        return {"status": "empty"}
    try:
        normalized = normalize_apply_url(apply_url)
    except UnparseableURL as exc:
        # Fires on every keystroke, so a URL is unparseable for most of the time
        # it takes to type one. A status, not an error.
        return {"status": "unparseable", "message": str(exc)}
    ats_type, ats_slug = detect_ats(apply_url)
    existing = queries.find_job_by_url(conn, apply_url)
    payload: dict[str, Any] = {
        "status": "new",
        "normalized": normalized,
        "ats_type": ats_type,
        "ats_slug": ats_slug,
    }
    if existing:
        application = queries.application_for_job(conn, int(existing["id"]))
        payload["status"] = "duplicate"
        payload["job"] = dict(existing)
        payload["application"] = dict(application) if application else None
    return payload


@router.post("/applications", status_code=201)
def create_application(conn: Conn, payload: ApplicationCreate) -> dict[str, Any]:
    try:
        application_id = actions.create_application(conn, payload.model_dump())
    except actions.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": application_id}


@router.get("/applications/{application_id}")
def application_detail(conn: Conn, application_id: int) -> dict[str, Any]:
    try:
        return views.detail_view(conn, application_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/applications/{application_id}/transition")
def post_transition(
    conn: Conn, application_id: int, payload: TransitionRequest
) -> dict[str, Any]:
    try:
        actions.transition(conn, application_id, payload.to_state, payload.detail)
    except actions.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return views.state_panel(conn, application_id)


@router.post("/applications/{application_id}/note")
def post_note(conn: Conn, application_id: int, payload: NoteRequest) -> dict[str, Any]:
    try:
        actions.add_note(conn, application_id, payload.detail)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return views.state_panel(conn, application_id)


@router.post("/applications/{application_id}/honesty")
def post_honesty(
    conn: Conn, application_id: int, payload: HonestyRequest
) -> dict[str, Any]:
    # Returns None, and an UPDATE against a missing id matches no rows rather
    # than failing — so the 404 comes from state_panel below, same as the form.
    try:
        queries.set_would_apply_anyway(conn, application_id, payload.would_apply_anyway)
    except ValueError as exc:  # the guard in queries raises on anything but 0/1
        raise HTTPException(422, str(exc)) from exc
    try:
        return views.state_panel(conn, application_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


# ------------------------------------------------------------------- review ---


@router.get("/review")
def review(
    conn: Conn, limit: Annotated[int, Query()] = views.REVIEW_LIMIT
) -> dict[str, Any]:
    limit = max(1, min(limit, 50))
    batch, waiting = views.review_batch(conn, limit)
    return {"batch": batch, "waiting": waiting, "limit": limit}


def _decide(conn: sqlite3.Connection, application_id: int, to_state: str) -> dict[str, Any]:
    try:
        decision = actions.decide(conn, application_id, to_state)
    except actions.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "application_id": decision.application_id,
        "title": decision.title,
        "outcome": decision.outcome,
        "siblings_closed": decision.siblings,
    }


@router.post("/review/{application_id}/approve")
def review_approve(conn: Conn, application_id: int) -> dict[str, Any]:
    from .. import states

    return _decide(conn, application_id, states.JOB_APPROVED)


@router.post("/review/{application_id}/skip")
def review_skip(conn: Conn, application_id: int) -> dict[str, Any]:
    from .. import states

    return _decide(conn, application_id, states.SKIPPED)


# ------------------------------------------------------------------- packet ---


def _packet_payload(
    conn: sqlite3.Connection, application_id: int, error: str | None = None
) -> dict[str, Any]:
    try:
        packet = views.packet_view(conn, application_id)
        resolved = views.answers_view(conn, application_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    # Record the gaps on every view, not only on build: the count is what says a
    # question belongs in facts.yaml, and the packet is seen more often than built.
    answers.flag_unknowns(conn, resolved["answers"], application_id)
    return {
        **packet,
        "answers": views.answers_json(resolved["answers"]),
        "unknowns": resolved["unknowns"],
        "error": error,
    }


@router.get("/packet/{application_id}")
def packet(conn: Conn, application_id: int) -> dict[str, Any]:
    return _packet_payload(conn, application_id)


@router.post("/packet/{application_id}/build")
def packet_build(conn: Conn, application_id: int) -> dict[str, Any]:
    """Tailor, render, store, advance. One model call, so it blocks."""
    try:
        error = actions.build_packet(conn, application_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _packet_payload(conn, application_id, error)


@router.post("/packet/{application_id}/answers/{key}/generate")
def generate_answer(conn: Conn, application_id: int, key: str) -> dict[str, Any]:
    try:
        actions.generate_answer(conn, application_id, key)
    except actions.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    resolved = views.answers_view(conn, application_id)
    return {
        "answers": views.answers_json(resolved["answers"]),
        "unknowns": resolved["unknowns"],
    }


@router.post("/packet/{application_id}/answers/{key}/set")
def set_answer(
    conn: Conn, application_id: int, key: str, payload: AnswerRequest
) -> dict[str, Any]:
    try:
        actions.set_answer(conn, application_id, key, payload.answer)
    except actions.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    resolved = views.answers_view(conn, application_id)
    return {
        "answers": views.answers_json(resolved["answers"]),
        "unknowns": resolved["unknowns"],
    }


@router.get("/packet/{application_id}/resume.pdf")
def packet_resume(conn: Conn, application_id: int) -> Response:
    """The stored bytes, not a re-render.

    `.claude/rules/data-layer.md`: `resume_pdf` is the record of what was actually
    sent. Serving a fresh render here would quietly make that false the first time
    a template changed.
    """
    pdf = queries.resume_pdf_bytes(conn, application_id)
    if pdf is None:
        raise HTTPException(404, "no packet built for this application")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="resume_{application_id}.pdf"'},
    )


# --------------------------------------------------------- fill / stats / etc ---


@router.get("/fill")
def fill(conn: Conn) -> dict[str, Any]:
    return views.fill_view(conn)


@router.get("/stats")
def stats(request: Request, conn: Conn) -> dict[str, Any]:
    return views.stats_view(conn, _params(request))


@router.post("/tailor")
def post_tailor(conn: Conn, payload: TailorRequest) -> dict[str, Any]:
    try:
        return actions.tailor_against(conn, payload.jd_text, payload.limit or 10)
    except actions.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/contacts")
def contacts(conn: Conn) -> dict[str, Any]:
    return {"contacts": queries.rows_to_dicts(queries.list_contacts(conn))}


@router.get("/corpus")
def corpus(conn: Conn) -> dict[str, Any]:
    return queries.corpus_counts(conn)


@router.get("/renders/{filename}")
def download_render(filename: str) -> Response:
    """Serve a rendered PDF. `out/` is disposable; the DB holds what was sent."""
    from fastapi.responses import FileResponse

    if "/" in filename or "\\" in filename or not filename.endswith(".pdf"):
        raise HTTPException(404, "no such file")
    path = (config.OUT_DIR / filename).resolve()
    if not path.is_file() or config.OUT_DIR.resolve() not in path.parents:
        raise HTTPException(404, "no such file")
    return FileResponse(path, media_type="application/pdf", filename=filename)
