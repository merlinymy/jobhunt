"""What a posting asks for that the corpus cannot support — and the nearest thing it can.

The resume already refuses to fabricate: a technology the corpus does not name is
an unsourced claim, so `validate` and `review` between them make it impossible to
answer "do you have AWS" by writing AWS. That is the right behaviour and it is not
the whole job, because the failure mode it leaves behind is silence. A reader
scanning for AWS who finds nothing about infrastructure at all concludes there is
none — when in fact there are containerised services, a Cloudflare tunnel, launchd
keeping things alive across reboots, and a month of clean uptime after a memory
leak. That is the answer. Nothing was assembling it.

Two things come out of this module and both matter:

  `bullet_ids` -> handed to the tailor, so the adjacent evidence lands **on the
                  resume**. Framed as a recommendation, never a requirement: a
                  slot spent on weak adjacent work is worse than a slot spent on
                  strong relevant work, and dropping is always allowed.
  `say`        -> the sentence to give a form or an interviewer, in the person's
                  own voice, grounded in the same rows.

Stored on the application rather than recomputed, because it depends only on the
posting and the corpus. A rebuild, a chat revision, and every later page load
reuse one call.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import config, llm, queries, tailor

# A posting that produces more than this is being read wrong — the model has
# started listing every noun rather than the requirements.
MAX_GAPS = 8


class GapError(RuntimeError):
    """The analysis could not be completed. Never fatal to a build."""


@dataclass
class Gap:
    wanted: str
    severity: str  # required | plus
    have: str
    bullet_ids: list[int] = field(default_factory=list)
    say: str = ""
    # Numbers in `say` that no corpus row contains. Empty is the normal case.
    unsourced: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "wanted": self.wanted,
            "severity": self.severity,
            "have": self.have,
            "bullet_ids": self.bullet_ids,
            "say": self.say,
            "unsourced": self.unsourced,
        }


def check_say(conn: sqlite3.Connection, gap: Gap, allowed: set[str] | None = None) -> list[str]:
    """Numbers in `say` that the corpus does not contain.

    `say` is the sentence handed to an interviewer, so a figure invented here is
    a fabrication with a person on the other end of it — the same failure the
    resume validator exists to prevent, arriving through a door that had no
    validator on it. Deterministic and free, so it runs on every gap.

    `allowed` is `tailor.corpus_numbers`, passed in when checking a batch so the
    corpus is read once for eight gaps rather than eight times.
    """
    return tailor.unsourced_numbers(
        gap.say, tailor.corpus_numbers(conn) if allowed is None else allowed
    )


def stored(conn: sqlite3.Connection, application_id: int) -> list[Gap] | None:
    """What was found last time, or None if never analysed."""
    row = conn.execute(
        "SELECT gaps FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None or row["gaps"] is None:
        return None
    try:
        parsed = json.loads(row["gaps"])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [
        Gap(
            wanted=str(g.get("wanted", "")),
            severity=str(g.get("severity", "required")),
            have=str(g.get("have", "")),
            bullet_ids=[int(i) for i in (g.get("bullet_ids") or []) if str(i).isdigit()],
            say=str(g.get("say", "")),
            unsourced=[str(u) for u in (g.get("unsourced") or [])],
        )
        for g in parsed
        if isinstance(g, dict)
    ]


def parse(conn: sqlite3.Connection, raw: str) -> list[Gap]:
    """Parse the reply, dropping anything that does not survive checking.

    Bullet ids are verified against the corpus rather than trusted. An id that
    is not a real row would otherwise reach the tailor as a recommendation to
    foreground work that does not exist — the same fabrication the validator
    catches downstream, arriving through a side door.
    """
    text = raw.strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise GapError(f"no JSON object in the reply: {raw[:200]!r}")
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GapError(f"the reply is not valid JSON: {exc}") from exc

    items = payload.get("gaps")
    if not isinstance(items, list):
        raise GapError("the reply has no `gaps` array")

    real = {int(b["id"]) for b in queries.corpus_bullets(conn)}
    allowed = tailor.corpus_numbers(conn)  # once for the batch, not once per gap
    out: list[Gap] = []
    for item in items[:MAX_GAPS]:
        if not isinstance(item, dict) or not str(item.get("wanted") or "").strip():
            continue
        ids = []
        for raw_id in item.get("bullet_ids") or []:
            try:
                bid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if bid in real:
                ids.append(bid)
        severity = str(item.get("severity") or "required").lower()
        gap = Gap(
            wanted=str(item["wanted"]).strip(),
            severity=severity if severity in ("required", "plus") else "required",
            have=str(item.get("have") or "").strip(),
            bullet_ids=ids,
            say=str(item.get("say") or "").strip(),
        )
        gap.unsourced = check_say(conn, gap, allowed)
        out.append(gap)
    return out


def analyse(
    conn: sqlite3.Connection,
    application_id: int,
    *,
    jd_text: str,
    on_progress: Any = None,
    refresh: bool = False,
) -> list[Gap]:
    """Find the gaps, or return what was found before.

    `refresh` forces a fresh call — for when the posting text has been replaced,
    which is the only thing that invalidates the answer.
    """
    if not refresh:
        existing = stored(conn, application_id)
        if existing is not None:
            return existing
    if not jd_text.strip():
        return []

    report = on_progress or (lambda **_: None)
    report(phase="gaps", message="checking the posting against your corpus")
    corpus, _ = tailor.build_prompt(conn, jd_text, limit=None)
    ask = (
        "JOB POSTING\n\n"
        f"{jd_text.strip()}\n\n"
        "Find what this posting asks for that the corpus above cannot support, "
        "and the closest thing it can. Return the JSON object described in your "
        "instructions."
    )
    raw = llm.complete(
        "gaps",
        ask,
        conn=conn,
        cached=corpus,
        application_id=application_id,
        expect_repeat=True,  # the tailor call follows immediately on the same corpus
    )
    found = parse(conn, raw)
    conn.execute(
        "UPDATE applications SET gaps = ?, updated_at = ? WHERE id = ?",
        (json.dumps([g.as_dict() for g in found]), config.utcnow(), application_id),
    )
    return found


def prompt_block(gaps: list[Gap]) -> str:
    """The hint handed to the tailor. Empty when there is nothing to say.

    Deliberately phrased as evidence rather than instruction. The tailor's own
    rules — one argument per line, the swap test, dropping is always allowed —
    still decide what earns a slot; this only says which rows answer a question
    the posting asked, so silence on the subject is at least a choice.
    """
    usable = [g for g in gaps if g.bullet_ids]
    if not usable:
        return ""
    lines = [
        "WHAT THIS POSTING ASKS FOR THAT THE CORPUS CANNOT SUPPORT",
        "",
        "You may not claim any of these. What follows each one is the nearest",
        "supported evidence, already checked against the corpus. A reader scanning",
        "for the missing thing and finding nothing on the subject concludes there is",
        "none, so these rows are worth a slot where they can earn one — on their own",
        "merits, under the usual rules. Do not bend a line to reach for the gap.",
        "",
    ]
    for g in usable:
        ids = ", ".join(str(i) for i in g.bullet_ids)
        lines.append(f"- wants {g.wanted} ({g.severity}); nearest: {g.have} [{ids}]")
    return "\n".join(lines)
