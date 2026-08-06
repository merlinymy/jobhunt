"""Writes shared by both renderers.

Same contract as `views.py`: no FastAPI, no `Request`, no HTMX. Failures raise
`Invalid` with the sentence a human should read, or `LookupError` for a row that
is not there. Each caller decides what those become on the wire.

These are here rather than in the routes because the validation is the expensive
part to get right — an outcome checked after the insert, a comp range checked
after the commit — and having one copy is the only way the form and the API stay
in step about it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .. import answers, config, db, llm, queries, resume, states, tailor
from ..normalize import detect_ats

# The outcomes the entry form can record, and the only legal successors of
# `applied`. Anything else is a typed-in mistake, not a state.
KNOWN_OUTCOMES = frozenset({states.REJECTED, states.INTERVIEW, states.OFFER})

# Comp is stored as a plain SQLite integer. Above this, int() still succeeds and
# the driver raises OverflowError at bind time, which was a 500 on the form.
MAX_COMP = 100_000_000


class Invalid(ValueError):
    """The input was rejected. The message is written to be shown as-is."""


class Conflict(Invalid):
    """Someone else already decided this. 409, not 422."""


@dataclass
class Decision:
    application_id: int
    title: str
    outcome: str
    siblings: int


def parse_comp(raw: str | int | None, field: str) -> int | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise Invalid(f"{field} should be a whole number.") from exc
    if not 0 <= value <= MAX_COMP:
        raise Invalid(f"{field} should be between 0 and {MAX_COMP:,}, got {value:,}.")
    return value


def create_application(conn: sqlite3.Connection, form: dict[str, Any]) -> int:
    """Log an application submitted by hand. Returns the new id.

    Seeds straight into `applied` — see the (None, APPLIED) note in states.py.
    """
    def text(key: str, default: str = "") -> str:
        value = form.get(key)
        return (str(value) if value is not None else default).strip()

    company, title, apply_url = text("company"), text("title"), text("apply_url")
    if not company or not title or not apply_url:
        raise Invalid("Company, title, and apply URL are all required.")

    outcome = text("outcome")
    # Checked before anything is written. This used to run after the application
    # was committed, so a value like "hired" raised InvalidTransition out of the
    # route: a 500 for the user, a saved row in the DB, and "Already tracked" on
    # the retry.
    if outcome and outcome not in KNOWN_OUTCOMES:
        raise Invalid(
            f"{outcome!r} is not an outcome I can record. "
            f"Pick one of: {', '.join(sorted(KNOWN_OUTCOMES))}."
        )

    applied_at = text("applied_at")
    # Validated, not interpolated — an unparseable date used to reach the column.
    try:
        applied_ts = config.date_to_utc(applied_at) if applied_at else config.utcnow()
    except ValueError as exc:
        raise Invalid(str(exc)) from exc

    comp_min = parse_comp(form.get("comp_min"), "Minimum comp")
    comp_max = parse_comp(form.get("comp_max"), "Maximum comp")
    if comp_min and comp_max and comp_min > comp_max:
        # Comp neither filters nor ranks — it exists to answer with. An inverted
        # range is a typo in a number that gets quoted on a form, so catch it.
        raise Invalid(f"Minimum comp ({comp_min:,}) is above maximum ({comp_max:,}).")

    referral = text("referral_contact_id")
    note = text("note")
    ats_type, ats_slug = detect_ats(apply_url)

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
                source=text("source") or "manual",
                location=text("location") or None,
                remote=text("remote") or "unknown",
                jd_text=text("jd_text") or None,
                comp_min=comp_min,
                comp_max=comp_max,
                posted_at=text("posted_at") or None,
            )
            application_id = states.create(
                conn,
                job_id=job_id,
                state=states.APPLIED,
                detail="manual entry",
                applied_at=applied_ts,
                would_apply_anyway=int(form.get("would_apply_anyway") or 0),
                referral_contact_id=int(referral) if referral else None,
            )
            if note:
                states.log_event(conn, application_id, "note", detail=note)
            # Inside the transaction with everything else: an outcome that cannot
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
        raise Invalid(str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        existing = queries.find_job_by_url(conn, apply_url)
        if existing:
            raise Invalid(
                f"Already tracked — {existing['title']} (job {existing['id']}). "
                "Update that application instead of logging a second one."
            ) from exc
        # A referral pointing at a contact that does not exist trips the foreign
        # key. Report it rather than letting the 500 surface.
        if "FOREIGN KEY" in str(exc).upper():
            raise Invalid("That referral contact no longer exists. Pick another, or none.") from exc
        raise Invalid(f"The database rejected that: {exc}") from exc
    except ValueError as exc:
        raise Invalid(str(exc)) from exc
    return application_id


def transition(
    conn: sqlite3.Connection, application_id: int, to_state: str, detail: str = ""
) -> None:
    try:
        states.transition(
            conn, application_id, to_state, detail=detail.strip() or "dashboard"
        )
    except states.InvalidTransition as exc:
        raise Invalid(str(exc)) from exc


def add_note(conn: sqlite3.Connection, application_id: int, detail: str) -> None:
    # Checked first: log_event would otherwise insert straight into `events` and
    # surface the foreign-key violation as a 500 rather than a 404.
    if queries.get_application(conn, application_id) is None:
        raise LookupError("no such application")
    if detail.strip():
        states.log_event(conn, application_id, "note", detail=detail.strip())


def decide(conn: sqlite3.Connection, application_id: int, to_state: str) -> Decision:
    """Approve or skip one review card, closing its duplicate listings.

    The same role in eight other cities is the same shot. Leaving them `scored`
    means they surface again tomorrow and the queue never drains; deciding them
    here is recorded as its own event naming the row that decided them, so
    nothing vanishes without a trace.
    """
    row = queries.get_application(conn, application_id)
    if row is None:
        raise LookupError("no such application")
    siblings = queries.sibling_application_ids(conn, application_id)
    try:
        states.transition(conn, application_id, to_state, detail="review queue")
    except states.InvalidTransition as exc:
        # Already decided elsewhere — two tabs, or a double-click. `Conflict` so
        # the caller can answer 409 and leave the card showing the real outcome.
        raise Conflict(str(exc)) from exc

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
    return Decision(
        application_id=application_id,
        title=row["title"],
        outcome="approved" if to_state == states.JOB_APPROVED else "skipped",
        siblings=closed,
    )


def build_packet(conn: sqlite3.Connection, application_id: int) -> str | None:
    """Tailor, render, store, advance. Returns an error sentence, or None.

    Synchronous — it is one model call. A fabrication rejection is the guarantee
    working, not a crash: nothing was rendered, the row stays `job_approved`, and
    it can be retried or done by hand. So it comes back as a message rather than
    an exception.
    """
    try:
        tailor.build_packet(conn, application_id)
    except LookupError:
        raise
    except tailor.NoJobDescription as exc:
        return str(exc)
    except (tailor.TailorError, llm.LLMError, resume.ResumeError) as exc:
        # FabricationError no longer arrives here: the dashboard builds with
        # `strict=False`, so a claim the corpus does not support is recorded on
        # the row as a finding and shown beside the line it is about. What is
        # left in this branch is genuinely unusable output — no bullets at all,
        # unparseable JSON, a failed render — where there is nothing to show.
        return f"{type(exc).__name__}: {exc}"
    return None


def generate_answer(conn: sqlite3.Connection, application_id: int, key: str) -> None:
    """Draft one narrative answer and cache it against the company."""
    question = answers.BY_KEY.get(key)
    if question is None:
        raise LookupError(f"no question {key!r}")
    row = queries.answer_context(conn, application_id)
    if row is None:
        raise LookupError("no such application")
    try:
        answers.generate(
            conn, question, company=row["company"], title=row["title"],
            jd_text=row["jd_text"], company_id=int(row["company_id"]),
            application_id=application_id,
        )
    except (answers.AnswerError, llm.LLMError) as exc:
        raise Invalid(str(exc)) from exc


def set_answer(conn: sqlite3.Connection, application_id: int, key: str, answer: str) -> None:
    """Store a per-company fact typed by hand — salary, and nothing else so far.

    Fact tier and `source = 'user'`, so it is returned verbatim to every later
    application to this employer. That is the point: quoting one company two
    different numbers is the failure this prevents.
    """
    question = answers.BY_KEY.get(key)
    if question is None:
        raise LookupError(f"no question {key!r}")
    if question.tier != answers.FACT:
        raise Invalid(f"{key!r} is narrative-tier; draft it instead")
    if not answer.strip():
        raise Invalid("an empty answer is not an answer")
    company_id = queries.company_id_for_application(conn, application_id)
    if company_id is None:
        raise LookupError("no such application")
    answers.put(conn, question.key, question.text, answer.strip(),
                tier=answers.FACT, source="user", company_id=company_id)


def tailor_against(
    conn: sqlite3.Connection, jd_text: str, limit: int = 10
) -> dict[str, Any]:
    """Tailor against a pasted JD and render a throwaway PDF into `out/`.

    The Phase 2 gate: .claude/rules/tailoring.md requires the diff before the
    resume is used, and requires that a diff which cannot be produced blocks the
    packet — so a validation failure comes back as a reason, never a PDF link.
    """
    import secrets

    if not jd_text.strip():
        raise Invalid("Paste a job description first.")
    try:
        result = tailor.tailor(
            conn,
            jd_text,
            limit=max(1, min(limit, 30)),
            # One JD pasted by hand. Nothing follows inside the 5-minute cache
            # TTL, so writing an entry here is a 125% charge against a read that
            # never happens. The queue worker is the caller that wants it.
            expect_repeat=False,
        )
    except tailor.FabricationError as exc:
        # Not a crash: this is the guarantee working, and seeing exactly what was
        # rejected is the point.
        raise Invalid(
            f"Rejected — the model asserted something the corpus does not support.\n\n{exc}"
        ) from exc
    # Named causes only. This was once `(TailorError, Exception)`, which is just
    # `Exception` — every bug in the request path rendered as a tidy 422 saying
    # tailoring failed, when the truth was a TypeError worth seeing.
    except (tailor.TailorError, llm.LLMError) as exc:
        raise Invalid(f"{type(exc).__name__}: {exc}") from exc

    # Second precision collided: two tailors inside the same second wrote the
    # same path and the second silently overwrote the first. `out/` is
    # disposable, but not while the tab is still open.
    stamp = config.utcnow().replace(":", "").replace("-", "")
    # No company name in the filename — see CLAUDE.local.md, Discretion.
    pdf_name = f"tailored_{stamp}_{secrets.token_hex(3)}.pdf"
    try:
        resume.render(
            resume.build_document(
                conn, selection=result.selection(), jd_text=jd_text,
                summary=result.summary,
            ),
            config.OUT_DIR / pdf_name,
        )
    except resume.ResumeError as exc:
        raise Invalid(f"Tailoring passed but the PDF did not render: {exc}") from exc

    return {
        "reasoning": result.reasoning,
        "diff": tailor.diff(result),
        "pdf": pdf_name,
        "kept": len(result.bullets),
        "reworded": sum(1 for b in result.bullets if b.changed),
    }
