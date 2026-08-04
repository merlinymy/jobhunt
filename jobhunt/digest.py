"""The 8am weekday Telegram digest: the top ~8 `scored` postings, with buttons.

This is the only place the system asks me for a decision, and the decision is
always mine — `score` ordered the list, it did not choose. Approve moves a
posting to `job_approved`; skip moves it to `skipped`. Nothing here advances a
state on its own.

Two modes, because Telegram is long-polling only (CLAUDE.md: no webhook, no port
forwarding, no tunnel, no static IP):

    make digest            build and send today's digest
    make digest ARGS=--poll   long-poll for button presses and apply them

Two separate guards, because one is not enough.

`events` has a partial unique index on `(application_id, kind) WHERE kind =
'digest_sent'`, so a posting is digested exactly once ever. That alone would
still let a second run send the *next* eight — which is not a duplicate, but it
does put sixteen postings in front of me on a morning I planned for eight. So
`send` also refuses when anything has already gone out today. The binding
constraint is my ten minutes per form, not the supply of postings, and quietly
doubling the queue is the exact failure this project exists to prevent.
`--force` overrides when I actually want more.

Ordering is location tier first, then score within the tier. That is
`docs/profile/scoring.yaml`'s rule and it matters: location *ranks* and never
rejects, so a remote posting scoring 70 should sit above an Atlanta posting
scoring 85. Sorting by score alone would quietly undo the whole location
preference.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import config, prefilter, states
from .db import connect, transaction

API = "https://api.telegram.org/bot{token}/{method}"
DEFAULT_LIMIT = 8

# Long-poll timeout. Telegram holds the connection open this long waiting for an
# update, which is what makes polling cheap — one request per 50s idle, not one
# per second. Their server caps it around 60.
POLL_TIMEOUT = 50


class DigestError(RuntimeError):
    """Telegram is unreachable, or not configured."""


def _credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise DigestError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env. "
            "Talk to @BotFather for a token; send your bot a message and read "
            "the chat id from https://api.telegram.org/bot<TOKEN>/getUpdates."
        )
    return token, chat_id


def call(method: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """One Telegram API call. Raises on anything that is not `ok: true`."""
    token, _ = _credentials()
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API.format(token=token, method=method),
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise DigestError(f"telegram {method} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise DigestError(f"telegram {method} failed: {type(exc).__name__}: {exc}") from exc
    if not parsed.get("ok"):
        raise DigestError(f"telegram {method} refused: {parsed.get('description')}")
    return parsed.get("result") or {}


# =================================== selecting ===================================


@dataclass
class Candidate:
    application_id: int
    title: str
    company: str
    score: float
    reason: str
    location: str | None
    remote: str | None
    apply_url: str
    comp_min: int | None
    comp_max: int | None
    tier: int
    referral: str | None


def candidates(conn: sqlite3.Connection, limit: int = DEFAULT_LIMIT) -> list[Candidate]:
    """Today's shortlist: highest-ranked `scored` postings never yet digested.

    The `NOT EXISTS` on `digest_sent` is the idempotence guard doing its work at
    selection time as well as at write time — without it a rerun would build the
    same eight, try to insert, and fail on the unique index instead of quietly
    sending nothing.
    """
    rows = conn.execute(
        """
        SELECT a.id AS application_id, a.score, a.score_reasoning,
               j.title, j.location, j.remote, j.apply_url, j.comp_min, j.comp_max,
               c.name AS company,
               (SELECT ct.name FROM contacts ct
                 WHERE ct.company_id = c.id AND ct.do_not_contact = 0
                 ORDER BY ct.id LIMIT 1) AS referral
          FROM applications a
          JOIN jobs j      ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.state = ?
           AND NOT EXISTS (SELECT 1 FROM events e
                            WHERE e.application_id = a.id AND e.kind = 'digest_sent')
        """,
        (states.SCORED,),
    ).fetchall()

    cfg = prefilter.load()
    ranked = [
        Candidate(
            application_id=int(row["application_id"]),
            title=row["title"],
            company=row["company"],
            score=float(row["score"] or 0),
            reason=row["score_reasoning"] or "",
            location=row["location"],
            remote=row["remote"],
            apply_url=row["apply_url"],
            comp_min=row["comp_min"],
            comp_max=row["comp_max"],
            tier=prefilter.location_tier(row, cfg),
            referral=row["referral"],
        )
        for row in rows
    ]
    # Location tier first, then score. Not score first — location ranks, and a
    # remote 70 really should outrank an Atlanta 85.
    ranked.sort(key=lambda c: (c.tier, -c.score))
    return ranked[:limit]


# =================================== rendering ===================================


def _escape(text: str) -> str:
    """Telegram MarkdownV2 is unforgiving; a stray `-` in a title kills the send."""
    for char in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(char, "\\" + char)
    return text


def _comp(candidate: Candidate) -> str | None:
    """Comp is shown, never filtered on. It exists to answer with."""
    if candidate.comp_min and candidate.comp_max:
        return f"${candidate.comp_min // 1000}k–${candidate.comp_max // 1000}k"
    if candidate.comp_min:
        return f"from ${candidate.comp_min // 1000}k"
    if candidate.comp_max:
        return f"up to ${candidate.comp_max // 1000}k"
    return None


def render(candidate: Candidate, index: int, total: int) -> str:
    where = candidate.location or "—"
    if (candidate.remote or "").lower() == "remote":
        where = f"Remote · {where}" if candidate.location else "Remote"
    lines = [
        f"*{_escape(candidate.title)}*",
        f"{_escape(candidate.company)} · {_escape(where)}",
        f"score {int(candidate.score)}",
    ]
    comp = _comp(candidate)
    if comp:
        lines[-1] += f" · {_escape(comp)}"
    if candidate.referral:
        # Surfaced because a referral changes whether a marginal posting is
        # worth ten minutes, which is the actual constraint.
        lines.append(f"🤝 referral: {_escape(candidate.referral)}")
    if candidate.reason:
        lines.append(f"_{_escape(candidate.reason)}_")
    lines.append(f"[apply]({candidate.apply_url})")
    lines.append(f"{index}/{total}")
    return "\n".join(lines)


def _keyboard(application_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"a:{application_id}"},
                {"text": "⏭ Skip", "callback_data": f"s:{application_id}"},
            ]
        ]
    }


# ==================================== sending ====================================


def sent_today(conn: sqlite3.Connection) -> int:
    """How many postings have already been digested today, UTC."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events "
        " WHERE kind = 'digest_sent' AND occurred_at >= ?",
        (f"{config.today()}T00:00:00Z",),
    ).fetchone()
    return int(row["n"])


def send(
    conn: sqlite3.Connection, limit: int = DEFAULT_LIMIT, *, force: bool = False
) -> dict[str, int]:
    """Send today's digest. Returns counts.

    Rerunning on a day that already has a digest sends nothing. The unique index
    on `digest_sent` guarantees no *posting* goes out twice, but that alone is
    not enough: a second run would simply select the next eight, so a launchd
    double-fire or a manual retry would put sixteen postings in front of me
    instead of eight. The gate is eight a weekday morning, and the binding
    constraint is my ten minutes a form — quietly doubling the queue is the
    exact failure this project exists to avoid. `--force` overrides.
    """
    _credentials()  # fail before selecting, not halfway through sending
    already = sent_today(conn)
    if already and not force:
        return {"sent": 0, "available": 0, "already_today": already}

    shortlist = candidates(conn, limit)
    if not shortlist:
        return {"sent": 0, "available": 0}

    _, chat_id = _credentials()
    sent = 0
    for index, candidate in enumerate(shortlist, start=1):
        try:
            call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": render(candidate, index, len(shortlist)),
                    "parse_mode": "MarkdownV2",
                    "link_preview_options": {"is_disabled": True},
                    "reply_markup": _keyboard(candidate.application_id),
                },
            )
        except DigestError as exc:
            # One posting that will not render must not cost the other seven.
            # No `digest_sent` row is written, so it is simply picked up
            # tomorrow rather than lost.
            print(f"  ! application {candidate.application_id}: {exc}", file=sys.stderr)
            continue
        # Written only after Telegram accepted it. The unique index means this
        # is also what stops a second run re-sending the same posting.
        with transaction(conn):
            states.log_event(
                conn,
                candidate.application_id,
                "digest_sent",
                detail=f"digest: score {candidate.score:.0f}, tier {candidate.tier}",
            )
        sent += 1
    return {"sent": sent, "available": len(shortlist)}


# =================================== polling ===================================


def _answer(callback_id: str, text: str) -> None:
    try:
        call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except DigestError:
        pass  # cosmetic toast; never worth failing the transition over


def handle_callback(conn: sqlite3.Connection, data: str) -> str:
    """Apply one button press. Returns the toast to show.

    Deliberately tolerant of a second press: Telegram redelivers on retry, and
    an already-approved posting should read as confirmation rather than an error.
    """
    action, _, raw_id = data.partition(":")
    try:
        application_id = int(raw_id)
    except ValueError:
        return "unrecognised button"
    target = {"a": states.JOB_APPROVED, "s": states.SKIPPED}.get(action)
    if target is None:
        return "unrecognised button"

    row = conn.execute(
        "SELECT state FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None:
        return "no such application"
    if row["state"] == target:
        return "already " + target
    try:
        with transaction(conn):
            states.transition(conn, application_id, target, detail="telegram digest")
    except states.InvalidTransition:
        return f"already {row['state']}"
    return "approved ✅" if target == states.JOB_APPROVED else "skipped"


def poll(conn: sqlite3.Connection, *, once: bool = False) -> int:
    """Long-poll `getUpdates` and apply button presses until interrupted.

    `offset` is the acknowledgement: Telegram redelivers an update until it is
    confirmed, so it advances only after the transition has committed. A crash
    mid-handle replays the press, which `handle_callback` treats as idempotent.
    """
    _credentials()
    offset: int | None = None
    handled = 0
    while True:
        payload: dict[str, Any] = {"timeout": POLL_TIMEOUT, "allowed_updates": ["callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            updates = call("getUpdates", payload, timeout=POLL_TIMEOUT + 15)
        except DigestError as exc:
            print(f"  ! {exc}", file=sys.stderr)
            time.sleep(5)
            continue

        for update in updates if isinstance(updates, list) else []:
            offset = int(update["update_id"]) + 1
            query = update.get("callback_query")
            if not query:
                continue
            result = handle_callback(conn, str(query.get("data", "")))
            _answer(str(query["id"]), result)
            handled += 1
            print(f"  {query.get('data')} -> {result}")
        if once:
            return handled


# =================================== driving ===================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the Telegram digest.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"how many postings to send (default {DEFAULT_LIMIT})")
    parser.add_argument("--poll", action="store_true",
                        help="long-poll for approve/skip presses instead of sending")
    parser.add_argument("--once", action="store_true",
                        help="with --poll, drain pending presses and exit")
    parser.add_argument("--force", action="store_true",
                        help="send even if a digest already went out today")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent; touches neither Telegram nor the DB")
    args = parser.parse_args(argv)

    conn = connect()
    try:
        if args.dry_run:
            shortlist = candidates(conn, args.limit)
            print(f"{len(shortlist)} posting(s) would be sent:\n")
            for index, candidate in enumerate(shortlist, start=1):
                print(render(candidate, index, len(shortlist)))
                print()
            return 0
        if args.poll:
            handled = poll(conn, once=args.once)
            print(f"handled {handled} button press(es)")
            return 0
        counts = send(conn, args.limit, force=args.force)
        if counts.get("already_today"):
            print(
                f"already sent {counts['already_today']} posting(s) today — nothing sent.\n"
                "Use --force to send another batch."
            )
        elif not counts["available"]:
            print("nothing to send — no `scored` postings that haven't been digested")
        else:
            print(f"sent {counts['sent']} of {counts['available']}")
    except DigestError as exc:
        print(f"digest not sent: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
