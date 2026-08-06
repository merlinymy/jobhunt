"""Select, reorder, and reword bullets for one job — then prove nothing was invented.

Read `.claude/rules/tailoring.md` before changing anything here. The short version:

  Permitted: selecting a subset, reordering, rewording, adjusting emphasis,
  changing which skills are foregrounded, tightening for length.

  Forbidden without exception: introducing an employer, title, date, degree,
  certification, or metric absent from the source rows. Inflating a number.
  Shifting a date. Turning shared credit into individual credit. Merging two
  bullets so the result implies a scope neither had.

There is no test suite elsewhere in this project. `validate()` is the substitute,
and it raises — it does not warn, and it does not quietly fall back to the
untailored resume. Adversarial fixtures live in tests/test_tailoring_validator.py.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any

from . import config, llm, queries, states
from .db import transaction
from .runs import ProgressFn

# Every number the resume asserts must come from its own source row. There is no
# allowance for "small" numbers: a blanket whitelist of 1-10 let `18%` become
# `5%` and let `Led 8 engineers` onto a bullet that mentions no team size, which
# are the two commonest ways a resume inflates.
#
# The one real exception is a digit that is part of an identifier rather than a
# quantity — `p99`, `S3`, `EC2`, `IPv4`. Those are distinguished structurally, by
# a letter immediately before the digits, so nothing has to be enumerated.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# The lookbehind has to exclude digits as well as letters. Blocking only letters
# rejects the `9` at the start of `p99` and then matches the second `9` on the
# next position, so `p99` reads as a bare `9` the source never mentions.
_STANDALONE_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*")
# The other half of that trade. Skipping an identifier stops `p99` reading as a
# bare `9`, but it also hid the token completely, so `p99` -> `p95` and `v1` ->
# `v4` were accepted: a changed percentile and a changed version, neither of them
# a claim the source makes. Uppercase ones were already caught as proper nouns
# (`Y537S` -> `Y538S`); lowercase ones matched nothing at all. So the token gets
# compared whole, which covers both cases and needs no case rule.
_GLUED_TOKEN = re.compile(r"[A-Za-z]+\d+[A-Za-z0-9]*")

# Numbers spelled out. Rule 2 is about claims, not digits, and "forty services"
# asserts exactly what "40 services" does. Mapped to digits so a source that
# writes one form licenses the other. `one` is deliberately absent: it is far
# commoner as ordinary prose ("one of", "no one") than as a count, and it can
# only ever understate.
_NUMBER_WORDS: dict[str, str] = {
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16",
    "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
    "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100", "thousand": "1000",
    "million": "1000000", "billion": "1000000000",
}

# The shared-credit regexes that lived here are gone the same way. Deciding
# whether "Led the migration" takes sole credit is a reading task, and `review()`
# is told to watch for exactly that — including the forms with no "I" in them,
# which is what the regex could never see.

# The scope-magnifier wordlist that lived here is gone — see the note where the
# check used to run, in `validate`.

# `bullets.metric` is handed to the model as `metric: …`, which reads as "this is
# the number worth quoting". For a good third of this corpus it is the opposite:
# a count of artifacts produced. "1,511 LOC", "240 commits", "9 profile fields".
# Those measure how much was typed, and a single-digit one makes the work sound
# smaller than it is — the recommendation is doing active harm, so it is withheld.
#
# Withheld from the prompt only. The value stays in the row, stays in the
# validator's allowed set, and stays usable if the bullet's own sentence happens
# to mention it. Nothing is deleted and nothing is newly forbidden; the model
# simply stops being told that this is the number to reach for.
#
# Two rules, because unit alone is not enough. Volume units are dropped at any
# magnitude — 36,200 LOC is not a bigger achievement than 973 LOC. Everything
# else is kept only if the number means something: a change, a share, a rate, a
# quantity in a real unit, or a figure large enough to be scale on its own.
_VOLUME_UNITS = re.compile(
    r"\b\d[\d,]*\s*(?:"
    r"LOC|lines?(?:\s+of\s+code)?|commits?|files?|modules?|packages?|"
    r"components?|endpoints?|routes?|pages?|tests?|classes|functions"
    r")\b",
    re.IGNORECASE,
)
_MEANINGFUL_NUMBER = re.compile(
    r"->|→|%|[<>≤≥]|[$£€]"                       # a change, a share, a threshold, money
    r"|\bbefore\b|\bafter\b"                     # an explicit comparison
    r"|\bof\s+[\d,]+"                            # "16 of 80" — a yield
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:ms|s|sec(?:s|onds?)?|min(?:s|utes?)?|h|hours?|"
    r"days?|weeks?|months?|years?|[KMGT]?B|bytes?|Å|x)\b",   # a real unit
    re.IGNORECASE,
)
# A bare count this large is scale in its own right — "1,703 postings",
# "~10,000 papers" — and reads as one.
_SCALE_THRESHOLD = 1000


def is_weak_metric(metric: str | None) -> bool:
    """Would showing this to the model recommend the wrong number?

    Public because `doctor` lists what it suppresses: a number silently dropped
    from every resume is worse than one printed, unless you can see the list and
    disagree with it.
    """
    text = (metric or "").strip()
    if not text:
        return False
    # Checked before the volume units, and the order is the whole point: a
    # *level* of volume says nothing ("2,511 lines TS/TSX"), but a *change* in
    # one is a real result ("1,334 -> 217 lines"). Deleting code is an
    # achievement; having written it is not.
    if _MEANINGFUL_NUMBER.search(text):
        return False
    if _VOLUME_UNITS.search(text):
        return True
    biggest = max(
        (int(m.group(0).replace(",", "")) for m in re.finditer(r"\d[\d,]*", text)),
        default=0,
    )
    return biggest < _SCALE_THRESHOLD


def weak_metrics(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """`(bullet id, metric)` for every metric being withheld from the prompt."""
    return [
        (int(row["id"]), str(row["metric"]))
        for row in conn.execute(
            "SELECT id, metric FROM bullets WHERE metric IS NOT NULL AND metric != ''"
        )
        if is_weak_metric(row["metric"])
    ]


class TailorError(RuntimeError):
    """The model's output is unusable."""


class FabricationError(TailorError):
    """The model asserted something the corpus does not support.

    This is the failure the whole design exists to catch. Whether it *blocks* is
    now the caller's choice: pass a `findings` list to `validate`, `review`, or
    either summary check and they record instead of raise. Omit it and they raise
    exactly as before, which is what the adversarial fixtures in
    `tests/test_tailoring_validator.py` assert and what `make tailor` still does.

    The dashboard passes a list, deliberately: every resume is read and edited by
    hand before it is sent, so a rejection that produces no document at all costs
    more than a warning printed beside the line it is about. The check did not get
    weaker — its findings just arrive as annotations instead of a dead end.
    """


@dataclass
class Finding:
    """One thing a check objected to, attached to the line it is about.

    `source` carries what the corpus actually says, because the useful question
    at review time is never "was this flagged" but "flagged against what".
    """

    kind: str  # number | identifier | noun | scope | solo | duplicate | unknown | review
    where: str  # "summary", or "bullet <id>"
    message: str
    bullet_id: int | None = None
    source: str = ""
    blocking: bool = False  # the bullet was dropped, not merely flagged

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "where": self.where,
            "message": self.message,
            "bullet_id": self.bullet_id,
            "source": self.source,
            "blocking": self.blocking,
        }


@dataclass
class TailoredBullet:
    bullet_id: int
    text: str
    source_text: str
    changed: bool = False


@dataclass
class TailorResult:
    bullets: list[TailoredBullet] = field(default_factory=list)
    reasoning: str = ""
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)

    def selection(self) -> dict[int, str]:
        """The shape `resume.build_cv` wants: bullet id -> final text, in order."""
        return {b.bullet_id: b.text for b in self.bullets}


# ================================== prompting ==================================


def build_prompt(
    conn: sqlite3.Connection,
    jd_text: str,
    *,
    limit: int | None = None,
    gap_block: str = "",
) -> tuple[str, str]:
    """Return `(corpus_block, prompt)`.

    The corpus block is byte-identical across every call, so it is passed
    separately and cached; only the JD and the instruction vary.
    """
    experiences = {int(r["id"]): r for r in queries.corpus_experiences(conn)}
    projects = {int(r["id"]): r for r in queries.corpus_projects(conn)}

    lines: list[str] = ["SOURCE BULLETS", ""]
    described: set[str] = set()
    for row in queries.corpus_bullets(conn):
        if row["experience_id"] is not None:
            parent = experiences.get(int(row["experience_id"]))
            where = f"{parent['company']} — {parent['title']}" if parent else "?"
        else:
            parent = projects.get(int(row["project_id"]))
            where = f"project: {parent['name']}" if parent else "?"
        # Emitted once per parent, before its first bullet. `role`, `scope`,
        # `traction` and the rest were loaded by 003 and then read by nothing —
        # they are what tells the model that a project had real users rather
        # than one, which is exactly the judgement selection turns on.
        if parent is not None and where not in described:
            described.add(where)
            context = _parent_context(parent)
            if context:
                lines.append(f"# {where} — {context}")
        flags = []
        # A metric that only counts artifacts is withheld — see `is_weak_metric`.
        # The bullet still appears; only the recommendation is dropped.
        if row["metric"] and not is_weak_metric(row["metric"]):
            flags.append(f"metric: {row['metric']}")
        flags.append(f"shared: {'false' if row['solo'] else 'true'}")
        lines.append(f"[{row['id']}] ({where}) [{'; '.join(flags)}]")
        lines.append(f"    {row['text']}")
    corpus = "\n".join(lines)

    ask = (
        "JOB DESCRIPTION\n\n"
        f"{jd_text.strip()}\n\n"
        # After the posting and before the instruction, deliberately: it is
        # commentary on what was just read, and it must not read as part of the
        # posting's own text.
        + (f"{gap_block}\n\n" if gap_block else "")
        + "Select the bullets that best evidence fitness for this role"
        + (f", at most {limit}" if limit else "")
        + ". Return the JSON object described in your instructions."
    )
    return corpus, ask


# Corpus columns that describe a parent rather than a bullet. Loaded by 003 and,
# until now, read by nothing: not the resume, which would be wrong for most of
# them ("personal use" is not a resume line), and not the prompt. They belong in
# the prompt, as background for choosing bullets.
_PARENT_CONTEXT_FIELDS = (
    ("role", "role"),
    ("scope", "scope"),
    ("traction", "traction"),
    ("known_for", "known for"),
    ("company_context", "company"),
    ("employment_type", "employment"),
)


def _parent_context(row: sqlite3.Row) -> str:
    """One line of background about an experience or project, or empty."""
    columns = row.keys()
    parts = [
        f"{label}: {str(row[name]).strip()}"
        for name, label in _PARENT_CONTEXT_FIELDS
        if name in columns and row[name]
    ]
    for name, label in (("titles_history", "titles"), ("recognition", "recognition")):
        if name in columns and row[name]:
            try:
                items = [str(item) for item in json.loads(row[name])]
            except json.JSONDecodeError:
                continue
            if items:
                parts.append(f"{label}: {', '.join(items)}")
    return " · ".join(parts)


def parse_response(raw: str) -> tuple[list[dict[str, Any]], str, str]:
    """Pull the JSON object out of the model's reply.

    Tolerates a fenced code block or surrounding prose, because a chatty reply is
    a formatting problem, not a fabrication. Anything else raises.
    """
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise TailorError(f"no JSON object in the model reply: {raw[:200]!r}")
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TailorError(f"model reply is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("bullets"), list):
        raise TailorError("model reply has no `bullets` array")
    return (
        payload["bullets"],
        str(payload.get("reasoning", "")).strip(),
        # Optional. A reply without one is a resume without a summary, not a
        # failed call — the prompt allows an empty string when the corpus does
        # not support one worth reading.
        str(payload.get("summary", "")).strip(),
    )


# ================================== validating ==================================


def _numbers(text: str, *, standalone_only: bool = False) -> list[str]:
    """Numeric tokens, normalized so `1,200` and `1200` compare equal.

    `standalone_only` skips digits glued to a preceding letter — the `99` in
    `p99`, the `2` in `EC2`. Used when reading the model's output, so an
    identifier is not mistaken for a claim.

    Reading the *source* keeps them, which is deliberately asymmetric: it means
    a source that says `p99` also licenses the output saying `99th percentile`.
    Every asymmetry here runs toward accepting sourced wording, never toward
    accepting an unsourced number.
    """
    pattern = _STANDALONE_NUMBER if standalone_only else _NUMBER
    return [match.group(0).replace(",", "") for match in pattern.finditer(text)]


def _spelled_numbers(text: str) -> set[str]:
    """Number words, as their digit form, so `forty` and `40` compare equal.

    The `rstrip(".")` is load-bearing and was missing. `_WORD` allows `.` inside
    a token on purpose, so `U.S.` survives — which also means a sentence ending
    in a spelled number tokenizes as `seven.`, that misses `_NUMBER_WORDS`, and
    the number never enters the allowed set. A real bullet ending "...and
    faithfulness at sixty-seven." therefore rejected its own near-verbatim
    rewording for inventing a `7`. The identifier check below compares it whole;
    this did not.
    """
    return {
        _NUMBER_WORDS[cleaned]
        for word in _WORD.findall(text)
        if (cleaned := word.lower().rstrip(".")) in _NUMBER_WORDS
    }


# A word, for proper-noun purposes. `+` and `#` stay so `C++` and `C#` survive;
# `-` does not, so `PhD-level` yields `PhD` rather than hiding it in a compound.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+.#']*")
# Capitalization carries no claim at the start of a sentence or clause.
_CLAUSE_START = re.compile(r"(?<=[.!?:;])\s+")

# The verb whitelist, the irregular list, the morphology check and the
# proper-noun scan that used to live here are gone, replaced by `review()`
# below. They were regex guessing at word class — is `Stopped` a verb or a
# company — and that is the one thing in this file a model does better than a
# pattern. Every false rejection this validator has produced came from them.
# `_stem` survives; the diff still uses it.


def _stem(word: str) -> str:
    """Crude suffix strip, enough to match inflections of the same source verb."""
    lowered = word.lower()
    for suffix in _INFLECTIONS:
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 3:
            return lowered[: -len(suffix)]
    return lowered


_TECH_COLUMNS = ("tech_built", "tech_owned", "tech_maintained", "tech_touched")


def validate(
    conn: sqlite3.Connection,
    emitted: list[dict[str, Any]],
    *,
    findings: list[Finding] | None = None,
) -> list[TailoredBullet]:
    """Check every rule in .claude/rules/tailoring.md.

    `findings=None` — the default, and what `make tailor` and the adversarial
    fixtures use — raises on the first failure and never returns partial results.

    Pass a list and the same checks record into it and carry on, so the caller
    gets a document plus a list of what to look at. Structural problems still
    raise or drop: an id the corpus does not have cannot be rendered, so it is
    removed and recorded as `blocking`, while a wording objection leaves the line
    in place and merely annotates it.
    """
    if not emitted:
        raise TailorError("the model selected no bullets")

    def objection(finding: Finding, exc: FabricationError) -> None:
        """Raise, or record. The one place the strict/lenient split lives."""
        if findings is None:
            raise exc
        findings.append(finding)

    ids: list[int] = []
    for index, item in enumerate(emitted):
        if not isinstance(item, dict) or "id" not in item or "text" not in item:
            raise TailorError(f"bullet {index} is missing `id` or `text`: {item!r}")
        try:
            ids.append(int(item["id"]))
        except (TypeError, ValueError) as exc:
            raise TailorError(f"bullet {index} has a non-numeric id: {item['id']!r}") from exc

    # 1. Every emitted bullet maps to a real row.
    sources = queries.bullets_by_id(conn, ids)
    missing = [bid for bid in ids if bid not in sources]
    if missing:
        objection(
            Finding(
                kind="unknown",
                where=f"bullet {missing[0]}" if len(missing) == 1 else "selection",
                message=(
                    f"dropped bullet id(s) {missing} — no such row in the corpus, so "
                    f"there is nothing to render or check them against"
                ),
                bullet_id=missing[0] if len(missing) == 1 else None,
                blocking=True,
            ),
            FabricationError(
                f"the model emitted bullet ids that are not in the corpus: {missing}. "
                "Every bullet on the resume must come from a `bullets` row."
            ),
        )
    noun_context = _noun_context(conn)
    duplicates = {bid for bid in ids if ids.count(bid) > 1}
    if duplicates:
        objection(
            Finding(
                kind="duplicate",
                where="selection",
                message=(
                    f"bullet id(s) {sorted(duplicates)} were used more than once; kept "
                    f"the first of each. One source row produces at most one line."
                ),
                blocking=True,
            ),
            FabricationError(
                f"bullet ids used more than once: {sorted(duplicates)}. "
                "One source row produces at most one resume line."
            ),
        )

    accepted: list[TailoredBullet] = []
    seen: set[int] = set()
    for item, bid in zip(emitted, ids):
        # Lenient mode reaches here with the unrenderable ones still in the list.
        if bid not in sources or bid in seen:
            continue
        seen.add(bid)
        source = sources[bid]
        text = str(item["text"]).strip()
        if not text:
            raise TailorError(f"bullet {bid} came back with empty text")
        haystack = f"{source['text']} {source['metric'] or ''}"

        # 2. Every number in the output appears in its own source row, whether
        #    written in digits or spelled out.
        allowed = set(_numbers(haystack)) | _spelled_numbers(haystack)
        written = set(_numbers(text, standalone_only=True)) | _spelled_numbers(text)
        invented = [n for n in written if n not in allowed]
        if invented:
            objection(
                Finding(
                    kind="number",
                    where=f"bullet {bid}",
                    message=(
                        f"number(s) {sorted(invented)} do not appear in the source row"
                    ),
                    bullet_id=bid,
                    source=source["text"],
                ),
                FabricationError(
                    f"bullet {bid} contains number(s) {sorted(invented)} that do not appear "
                    f"in its source row. Source: {source['text']!r}"
                ),
            )

        # Names a bullet may use beyond its own sentence — technologies recorded
        # against the same role or project. Rule 2 keeps numbers pinned to the
        # bullet itself; names are allowed to come from the parent, because
        # "changing which skills are foregrounded" is explicitly permitted.
        context = f"{haystack} {noun_context.get(bid, '')}"

        # 2b. Identifiers carrying a number — `p99`, `v1`, `Y537S`, `BM25`,
        #     `base64`. Rule 2 skips these on purpose (see _GLUED_TOKEN), so
        #     they are compared whole here instead. Changing one asserts a
        #     different percentile, version, or molecule.
        #
        #     Checked against the bullet's own row, not the widened context the
        #     name checks use. A digit inside a name is still a number, and rule
        #     2 pins numbers to their own bullet. The difference is load-bearing:
        #     ARC's tech list holds "Tailwind CSS v4", which was enough to let
        #     an ARC bullet reading "since the v1 launch" be reworded to "v4".
        sourced_tokens = {t.lower() for t in _GLUED_TOKEN.findall(haystack)}
        invented_tokens = sorted(
            {t for t in _GLUED_TOKEN.findall(text) if t.lower() not in sourced_tokens}
        )
        if invented_tokens:
            objection(
                Finding(
                    kind="identifier",
                    where=f"bullet {bid}",
                    message=(
                        f"identifier(s) {invented_tokens} are not in the source row — a "
                        f"digit inside a name is still a claim, `p95` is not `p99`"
                    ),
                    bullet_id=bid,
                    source=source["text"],
                ),
                FabricationError(
                    f"bullet {bid} uses identifier(s) {invented_tokens} that do not appear "
                    f"in its source row. A digit inside a name is still a claim — `p95` is "
                    f"not `p99` and `v4` is not `v1`. Source: {source['text']!r}"
                ),
            )

        # 2c. A non-ASCII letter the source never uses. `_WORD` is ASCII-anchored,
        #     so a Cyrillic Ѕ in `Ѕtripe` leaves `tripe` — a lowercase word the
        #     proper-noun check below skips — and a homoglyph is how a name gets
        #     past a name check. Not an ASCII-only rule: `ERα` is in the corpus,
        #     so the test is whether the source uses that character at all.
        foreign = sorted(
            {c for c in text if ord(c) > 127 and c.isalpha() and c not in context}
        )
        if foreign:
            objection(
                Finding(
                    kind="homoglyph",
                    where=f"bullet {bid}",
                    message=(
                        f"letter(s) {foreign} appear nowhere in the source row — a "
                        f"homoglyph reads as an ordinary word to every other check here"
                    ),
                    bullet_id=bid,
                    source=source["text"],
                ),
                FabricationError(
                    f"bullet {bid} contains letter(s) {foreign} that appear nowhere in its "
                    f"source row — a homoglyph reads as an ordinary word to every check "
                    f"here. Source: {source['text']!r}"
                ),
            )

        # 3 and 4. Employers, titles, dates, degrees and schools are structural:
        # they come from `experiences` and `education`, never from bullet text.
        # Any proper noun the source row does not contain is an invention.
        # Everything that is a reading task — an invented employer, a category
        # word standing in for a specific one, scope widened, credit taken —
        # is `review()`'s job, run once over the whole set after this loop.

        accepted.append(
            TailoredBullet(
                bullet_id=bid,
                text=text,
                source_text=source["text"],
                changed=text.strip() != source["text"].strip(),
            )
        )
    return accepted


def _json_names(row: sqlite3.Row, columns: tuple[str, ...]) -> list[str]:
    """Values out of the loader's JSON-array columns — tech lists, bullet skills.

    One reader, so a malformed column degrades the same way everywhere: silently
    to nothing, because a corpus with an unparseable list should narrow what may
    be claimed, never widen it.
    """
    out: list[str] = []
    for column in columns:
        if column in row.keys() and row[column]:
            try:
                out.extend(str(item) for item in json.loads(row[column]))
            except json.JSONDecodeError:
                pass
    return out


def _tech_names(row: sqlite3.Row) -> list[str]:
    return _json_names(row, _TECH_COLUMNS)


def _corpus_haystack(conn: sqlite3.Connection) -> str:
    """Everything the corpus asserts, as one blob to check the summary against.

    A summary is prose about the person rather than about one piece of work, so
    it has no single source row to be pinned to — but "unsourced" still has to
    mean something or the section is a hole in the guarantee. The whole corpus
    is the right scope: it may say anything the profile already says, in any
    combination, and nothing else.

    Parent background is gathered **per parent**. It used to be
    `_noun_context(conn).values()`, which keys the same digest to every bullet
    under a parent — so a 117-bullet corpus repeated itself 117 times and this
    returned 703 KB, of which 672 KB was duplication. That mattered because
    `review_summary` then truncated to the first 12 KB and judged every summary
    against 1.7% of the corpus, rejecting correct claims sourced from the rest.
    Coverage here is unchanged; only the repetition is gone.
    """
    parts: list[str] = []
    for row in queries.corpus_bullets(conn):
        parts.append(str(row["text"] or ""))
        if "metric" in row.keys() and row["metric"]:
            parts.append(str(row["metric"]))
        # `skills` is where a third of the corpus's vocabulary lives — "adversarial
        # validation", "connection pooling", "provider abstraction". A summary may
        # name any of it, so leaving it out here would reject correct summaries.
        parts.extend(_json_names(row, ("skills",)))
    for row in queries.corpus_experiences(conn):
        for column in ("company", "title", "scope", "company_context"):
            if column in row.keys() and row[column]:
                parts.append(str(row[column]))
    for row in queries.corpus_projects(conn):
        for column in ("name", "role", "blurb"):
            if column in row.keys() and row[column]:
                parts.append(str(row[column]))
    for row in queries.corpus_education(conn):
        for column in ("school", "degree", "field"):
            if column in row.keys() and row[column]:
                parts.append(str(row[column]))
    for row in queries.corpus_experiences(conn):
        parts.extend(_tech_names(row))
        parts.append(_parent_context(row))
    for row in queries.corpus_projects(conn):
        parts.extend(_tech_names(row))
        parts.append(_parent_context(row))
    return " ".join(parts)


def validate_summary(
    conn: sqlite3.Connection, summary: str, *, findings: list[Finding] | None = None
) -> str:
    """Check the resume summary the same way a bullet is checked, corpus-wide.

    Deliberately the same machinery rather than a softer pass. A summary is the
    single easiest place on a resume to acquire a seniority label or a round
    number of years that nothing supports — it is written in the register that
    invites exactly that — so it gets the number check, the identifier check and
    the proper-noun check, just with the whole corpus as the source.

    Returns the summary, or raises. An empty one is fine: the prompt is told to
    return nothing rather than pad, and a resume with no summary is a resume.
    """
    text = summary.strip()
    if not text:
        return ""

    def objection(finding: Finding, exc: FabricationError) -> None:
        if findings is None:
            raise exc
        findings.append(finding)

    haystack = _corpus_haystack(conn)
    allowed = set(_numbers(haystack)) | _spelled_numbers(haystack)
    written = set(_numbers(text, standalone_only=True)) | _spelled_numbers(text)
    invented = sorted(n for n in written if n not in allowed)
    if invented:
        objection(
            Finding(
                kind="number",
                where="summary",
                message=(
                    f"number(s) {invented} appear nowhere in the corpus — years of "
                    f"experience and team sizes are the usual culprits, and exactly "
                    f"the claims a reader checks"
                ),
            ),
            FabricationError(
                f"the summary contains number(s) {invented} that appear nowhere in the "
                f"corpus. Years of experience and team sizes are the usual culprits, and "
                f"they are exactly the claims a reader checks. Summary: {text!r}"
            ),
        )

    sourced_tokens = {t.lower() for t in _GLUED_TOKEN.findall(haystack)}
    invented_tokens = sorted(
        {t for t in _GLUED_TOKEN.findall(text) if t.lower() not in sourced_tokens}
    )
    if invented_tokens:
        objection(
            Finding(
                kind="identifier",
                where="summary",
                message=(
                    f"identifier(s) {invented_tokens} appear nowhere in the corpus"
                ),
            ),
            FabricationError(
                f"the summary uses identifier(s) {invented_tokens} that appear nowhere in "
                f"the corpus. Summary: {text!r}"
            ),
        )

    # Names, seniority and scope in the summary go to `review_summary()`; this
    # function keeps only what is arithmetic.
    return text


def _noun_context(conn: sqlite3.Connection) -> dict[int, str]:
    """Proper nouns each bullet is allowed to use, beyond its own sentence.

    Scoped deliberately. Numbers stay pinned to the bullet's own text because
    rule 2 says "its own bullet" — a metric borrowed from a neighbour is a lie.
    Names are different: `.claude/rules/tailoring.md` permits "changing which
    skills are foregrounded", so a technology recorded against the same role or
    project is fair to name even when that one sentence omits it. What stays
    forbidden is a name that appears nowhere in the corpus at all.

    Without this the validator blocks an operation the rules explicitly allow —
    it rejected `React` on a bullet from a role whose tech list contains React.
    """
    def words(row: sqlite3.Row, *extra: str) -> str:
        parts = [str(row[name]) for name in extra if name in row.keys() and row[name]]
        parts.extend(_tech_names(row))
        # The same background `build_prompt` shows the model. It has to be
        # allowed here too, or naming something the prompt itself supplied
        # would be rejected as an invention.
        parts.append(_parent_context(row))
        return " ".join(parts)

    experience_context = {
        int(row["id"]): words(row, "company", "title") for row in queries.corpus_experiences(conn)
    }
    project_context = {
        int(row["id"]): words(row, "name") for row in queries.corpus_projects(conn)
    }

    # Every bullet under one parent, pooled. A name the role or project
    # demonstrably uses is fair on any of its bullets, because "changing which
    # skills are foregrounded" is exactly what the rules permit.
    #
    # Without this the validator rejected `Chained an LLM query path` on an ARC
    # bullet while four other ARC bullets say LLM — which is not a fabrication,
    # it is the tailoring the tailor exists to do. Three consecutive real
    # packet builds died this way, and the only output that survived was
    # near-verbatim, which is untailored by definition.
    #
    # What stays forbidden is unchanged and is the case that matters: a name
    # from a *different* parent, or from nowhere in the corpus at all. Grafana
    # on a bullet from the role that never touched Grafana is still an invention.
    sibling_text: dict[tuple[str, int], list[str]] = {}
    bullets = queries.corpus_bullets(conn)
    for row in bullets:
        key = ("e", int(row["experience_id"])) if row["experience_id"] is not None \
            else ("p", int(row["project_id"]))
        sibling_text.setdefault(key, []).append(str(row["text"]))

    context: dict[int, str] = {}
    for row in bullets:
        if row["experience_id"] is not None:
            key = ("e", int(row["experience_id"]))
            parent = experience_context.get(key[1], "")
        else:
            key = ("p", int(row["project_id"]))
            parent = project_context.get(key[1], "")
        own_skills = ""
        if row["skills"]:
            try:
                own_skills = " ".join(str(item) for item in json.loads(row["skills"]))
            except json.JSONDecodeError:
                pass
        siblings = " ".join(sibling_text.get(key, ()))
        context[int(row["id"])] = f"{parent} {own_skills} {siblings}"
    return context


# ================================== reviewing ==================================
#
# The half of validation that is a reading task rather than a comparison.
#
# The split is the design. Numbers, identifiers and homoglyphs stay in `validate`
# because they are exact string comparisons, where a regex is perfect and free
# and a model is unreliable — asked whether `18%` appears in a source that says
# `15%`, a model will sometimes say yes, and a shifted percentile is invisible in
# a diff. Everything else — is this an invented employer or an ordinary verb, is
# "an LLM" a fair swap for "Claude", has one service become the platform — is
# reading, where a regex was guessing at word class and produced every false
# rejection this validator has ever made.


class ReviewUnavailable(TailorError):
    """The check could not be run. Distinct from the check failing."""


def _parent_digest(conn: sqlite3.Connection) -> dict[int, str]:
    """Bullet id -> a one-line summary of what its role or project is allowed to
    name: the parent's own label and the technologies recorded against it.

    Deliberately not `_noun_context`, which pools the full prose of every
    sibling bullet. That is the right shape for a regex haystack and the wrong
    shape for a reader, twice over. It was 95% of the checker's input — 76 KB a
    call — and it hands the checker another bullet's claims as if they were
    support for this one, which is the opposite of what a per-line check is for.
    The rules permit naming a technology *recorded against* the parent; that is
    a short structured list, so send that.
    """
    tech_columns = ("tech_built", "tech_owned", "tech_maintained", "tech_touched")

    def digest(row: sqlite3.Row, label: str) -> str:
        names: list[str] = []
        for column in tech_columns:
            if column in row.keys() and row[column]:
                try:
                    names.extend(str(item) for item in json.loads(row[column]))
                except json.JSONDecodeError:
                    pass
        unique = list(dict.fromkeys(names))
        return f"{label} — technologies: {', '.join(unique)}" if unique else label

    by_experience = {
        int(row["id"]): digest(row, f"{row['company']}, {row['title']}")
        for row in queries.corpus_experiences(conn)
    }
    by_project = {
        int(row["id"]): digest(row, f"project: {row['name']}")
        for row in queries.corpus_projects(conn)
    }

    out: dict[int, str] = {}
    for row in queries.corpus_bullets(conn):
        if row["experience_id"] is not None:
            found = by_experience.get(int(row["experience_id"]))
        else:
            found = by_project.get(int(row["project_id"]))
        if found:
            out[int(row["id"])] = found
    return out


def _review_pairs(pairs: list[tuple[int, str, str, str]]) -> str:
    """`(n, line, source, parent)` -> the checker's user message.

    No job description, and that is deliberate: this is a comparison, not a
    judgement. A checker that knows which job is being applied for can be talked
    into a claim by how well it fits the posting, and a checker asked only
    "does the source say this" cannot.
    """
    blocks = []
    for number, line, source, parent in pairs:
        block = [f"--- {number} ---", f"SOURCE: {source}"]
        if parent.strip():
            block.append(f"PARENT: {parent.strip()}")
        block.append(f"LINE: {line}")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks) + (
        f"\n\nReturn a verdict for each of the {len(pairs)} pairs above."
    )


def _parse_verdicts(raw: str, expected: set[int]) -> dict[int, dict[str, Any]]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ReviewUnavailable(f"the checker returned no JSON: {raw[:200]!r}")
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewUnavailable(f"the checker's reply is not valid JSON: {exc}") from exc
    verdicts = payload.get("verdicts")
    if not isinstance(verdicts, list):
        raise ReviewUnavailable("the checker's reply has no `verdicts` array")

    out: dict[int, dict[str, Any]] = {}
    for entry in verdicts:
        if isinstance(entry, dict) and "n" in entry:
            try:
                out[int(entry["n"])] = entry
            except (TypeError, ValueError):
                continue
    # A line the checker skipped is a line nothing looked at. Refusing to
    # proceed is the only honest option: silently treating a missing verdict as
    # a pass turns a truncated reply into an unchecked resume.
    unjudged = sorted(expected - set(out))
    if unjudged:
        raise ReviewUnavailable(
            f"the checker returned no verdict for {unjudged}. Nothing looked at "
            f"{'that line' if len(unjudged) == 1 else 'those lines'}."
        )
    return out


def review(
    conn: sqlite3.Connection,
    accepted: list[TailoredBullet],
    *,
    application_id: int | None = None,
    findings: list[Finding] | None = None,
) -> None:
    """Read every line against its own source row.

    `findings=None` raises on the first violation. Pass a list and every verdict
    is recorded instead, so one build reports all of them rather than the first.

    Runs on a different model from the one that wrote the lines — see the note
    on `tailor_check` in models.yaml. Two calls to one model share its blind
    spots.
    """
    if not accepted:
        return
    sources = queries.bullets_by_id(conn, [b.bullet_id for b in accepted])
    parents = _parent_digest(conn)
    pairs: list[tuple[int, str, str, str]] = []
    for number, bullet in enumerate(accepted, start=1):
        row = sources.get(bullet.bullet_id)
        source_text = bullet.source_text
        if row is not None and row["metric"]:
            source_text = f"{source_text} ({row['metric']})"
        if row is not None and not row["solo"]:
            source_text = f"{source_text} [shared work — others were involved]"
        pairs.append(
            (number, bullet.text, source_text, parents.get(bullet.bullet_id, ""))
        )

    try:
        raw = llm.complete(
            "tailor_check",
            _review_pairs(pairs),
            conn=conn,
            application_id=application_id,
            expect_repeat=False,
        )
    except llm.LLMError as exc:
        # Fails closed, but as its own error. A packet build is retryable and
        # not urgent; quietly shipping an unchecked resume because the API
        # blipped is the one outcome worth refusing.
        #
        # "did not complete" rather than "could not be reached": the first real
        # failure here was a truncated reply, and the call had very much been
        # reached. `llm.LLMError` covers both, and its own message says which.
        raise ReviewUnavailable(f"the check did not complete: {exc}") from exc

    verdicts = _parse_verdicts(raw, {n for n, _, _, _ in pairs})
    for number, line, source_text, _ in pairs:
        verdict = verdicts[number]
        if verdict.get("ok") is True:
            continue
        claim = str(verdict.get("claim") or "").strip()
        why = str(verdict.get("why") or "").strip()
        exc = FabricationError(
            f"line {number} claims something its source does not support"
            + (f" — {claim!r}" if claim else "")
            + f".\n\n{why}" * bool(why)
            + f"\n\nLine:   {line}\nSource: {source_text}"
        )
        if findings is None:
            raise exc
        bullet = accepted[number - 1]
        findings.append(
            Finding(
                kind="review",
                where=f"bullet {bullet.bullet_id}",
                message=(f"{claim!r} — {why}" if claim and why else why or str(exc)),
                bullet_id=bullet.bullet_id,
                source=source_text,
            )
        )


def review_summary(
    conn: sqlite3.Connection,
    summary: str,
    *,
    application_id: int | None = None,
    findings: list[Finding] | None = None,
) -> None:
    """The same read, with the whole corpus as the source.

    A summary has no single source row, so the corpus digest stands in for one.
    It is the one place on a resume that invites a seniority label or a round
    number of years out of nowhere, which is a reading task end to end.
    """
    if not summary.strip():
        return
    # The whole corpus, untruncated. This used to be `[:12000]` against a
    # haystack that repeated itself 117 times and ran to 703 KB, so the checker
    # saw 1.7% of the profile and rejected anything sourced from the rest — a
    # correct summary citing a real client result came back as "unsupported by
    # the source" because the client was in the 98% it never received. The
    # haystack is now ~38 KB with identical coverage, which is one call.
    pairs = [(1, summary.strip(), _corpus_haystack(conn), "")]
    try:
        raw = llm.complete(
            "tailor_check",
            _review_pairs(pairs),
            conn=conn,
            application_id=application_id,
            expect_repeat=False,
        )
    except llm.LLMError as exc:
        raise ReviewUnavailable(f"the check did not complete: {exc}") from exc

    verdict = _parse_verdicts(raw, {1})[1]
    if verdict.get("ok") is not True:
        claim = str(verdict.get("claim") or "").strip()
        why = str(verdict.get("why") or "").strip()
        exc = FabricationError(
            "the summary claims something the corpus does not support"
            + (f" — {claim!r}" if claim else "")
            + f".\n\n{why}" * bool(why)
            + f"\n\nSummary: {summary.strip()}"
        )
        if findings is None:
            raise exc
        findings.append(
            Finding(
                kind="review",
                where="summary",
                message=(f"{claim!r} — {why}" if claim and why else why or str(exc)),
            )
        )


# =================================== driving ===================================


def tailor(
    conn: sqlite3.Connection,
    jd_text: str,
    *,
    limit: int | None = 12,
    application_id: int | None = None,
    expect_repeat: bool = True,
    strict: bool = True,
    on_progress: ProgressFn | None = None,
    gap_block: str = "",
) -> TailorResult:
    """One tailoring pass: prompt, parse, check.

    `strict=True` — what `make tailor` uses — raises unless every check holds,
    retrying once with the complaint handed back.

    `strict=False` — what the dashboard uses — always returns a document, with
    every objection collected into `result.findings` for a human to act on. There
    is no retry in that mode: a retry exists to turn a dead end into a resume, and
    without a dead end it is just a second bill for a wording the reader can fix
    in the editor faster than the model can.

    `expect_repeat` passes straight to `llm.complete` — leave it True when
    working through a queue, set it False for a single hand-pasted JD.
    """
    if not jd_text.strip():
        raise TailorError("no job description to tailor against")
    report: ProgressFn = on_progress or (lambda **_: None)
    corpus, prompt = build_prompt(conn, jd_text, limit=limit, gap_block=gap_block)

    # One retry, with the validator's own complaint handed back. A rejection is
    # usually a single word — a category term the source never used, a number
    # reworded into a form the corpus does not contain — and the model fixes it
    # immediately when told exactly what tripped. Without this a rejection is a
    # dead end: no resume, and the only wording that reliably survives is
    # verbatim, which is untailored by definition.
    #
    # Exactly one retry. The validator is the guarantee, and a loop that keeps
    # asking until something passes is a loop that eventually accepts whatever
    # the model was most determined to say.
    if not strict:
        found: list[Finding] = []
        report(phase="selecting", message="reading the posting against your corpus")
        raw = llm.complete(
            "tailor",
            prompt,
            conn=conn,
            cached=corpus,
            application_id=application_id,
            expect_repeat=expect_repeat,
        )
        emitted, reasoning, summary = parse_response(raw)
        bullets = validate(conn, emitted, findings=found)
        checked_summary = validate_summary(conn, summary, findings=found)
        report(
            phase="checking",
            message=f"reading {len(bullets)} line{'' if len(bullets) == 1 else 's'} back against their source rows",
        )
        # ReviewUnavailable is not a fabrication — the checker never ran. Record
        # that plainly rather than letting a network blip read as a clean bill.
        try:
            review(conn, bullets, application_id=application_id, findings=found)
            review_summary(
                conn, checked_summary, application_id=application_id, findings=found
            )
        except ReviewUnavailable as exc:
            found.append(
                Finding(
                    kind="unchecked",
                    where="selection",
                    message=f"the claim checker did not run, so nothing here was read against its source: {exc}",
                )
            )
        return TailorResult(
            bullets=bullets,
            reasoning=reasoning,
            summary=checked_summary,
            findings=found,
        )

    attempt_prompt = prompt
    last: FabricationError | None = None
    for attempt in (1, 2):
        raw = llm.complete(
            "tailor",
            attempt_prompt,
            conn=conn,
            cached=corpus,
            application_id=application_id,
            expect_repeat=expect_repeat,
        )
        emitted, reasoning, summary = parse_response(raw)
        try:
            # Arithmetic first, and deliberately: it is free, deterministic, and
            # a set of lines that already invents a number is not worth paying
            # a second model to read.
            bullets = validate(conn, emitted)
            checked_summary = validate_summary(conn, summary)
            review(conn, bullets, application_id=application_id)
            review_summary(conn, checked_summary, application_id=application_id)
            return TailorResult(
                bullets=bullets, reasoning=reasoning, summary=checked_summary
            )
        except FabricationError as exc:
            if attempt == 2:
                raise
            last = exc
            attempt_prompt = (
                f"{prompt}\n\nYour previous answer was REJECTED by the validator:\n\n"
                f"{exc}\n\nFix only that. Every other bullet was fine. Use the "
                f"source row's own words for whatever tripped it, or drop the "
                f"claim — dropping is always allowed, adding never is."
            )
    raise last or TailorError("tailoring failed")  # pragma: no cover


# ==================================== diff ====================================


def diff(result: TailorResult) -> list[dict[str, Any]]:
    """Every change against master, for the dashboard.

    `.claude/rules/tailoring.md`: if a diff cannot be produced, that is a failure
    and the packet is blocked. So this is derived from the validated result,
    which already carries each bullet's source text.
    """
    return [
        {
            "bullet_id": b.bullet_id,
            "before": b.source_text,
            "after": b.text,
            "changed": b.changed,
        }
        for b in result.bullets
    ]


# =================================== packets ===================================


class NoJobDescription(TailorError):
    """The posting has no JD stored, so there is nothing to tailor against."""


# A packet can be built from `job_approved` and rebuilt from `packet_ready`.
# states.py already takes this position and says why: `packet_ready` is kept out
# of SUBMITTED_STATES because "a built packet is reproducible — rerun `make
# tailor`". The guard here used to demand `job_approved` exactly, which made that
# false, and made every prompt edit unevaluable against a job already approved.
BUILDABLE_STATES = frozenset({states.JOB_APPROVED, states.PACKET_READY})


def log_gap_failure(application_id: int, exc: Exception) -> None:
    """A failed gap analysis is a missing improvement, not a broken build."""
    import logging

    logging.getLogger("uvicorn.error").warning(
        "jobhunt: gap analysis skipped for %s — %s: %s",
        application_id,
        type(exc).__name__,
        exc,
    )


def build_packet(
    conn: sqlite3.Connection,
    application_id: int,
    *,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """`job_approved` -> tailored PDF stored on the row -> `packet_ready`.

    Rebuilding a `packet_ready` row is allowed and replaces the stored PDF: it
    has not been sent anywhere, and iterating on the prompt is worth nothing if
    it cannot be tried against a real posting. The row stays `packet_ready` and
    the overwrite is logged as an event, so the history says a resume was
    replaced rather than silently differing from the one seen yesterday.

    Once the application reaches `applied`, this refuses. From that point
    `resume_pdf` is the record of what an employer actually received, and
    `.claude/rules/data-layer.md` is unconditional about it: never reconstruct a
    past submission from current templates. Nothing can regenerate the exact
    bytes that went out, so nothing may overwrite them.

    Rendering happens before the transition and inside the same transaction as
    it, so a failed render leaves the row in `job_approved` to retry rather than
    in `packet_ready` with nothing attached.
    """
    row = conn.execute(
        """
        SELECT a.id, a.state, j.title, j.jd_text, c.name AS company
          FROM applications a
          JOIN jobs j      ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.id = ?
        """,
        (application_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no application {application_id}")
    if row["state"] in states.SUBMITTED_STATES:
        raise TailorError(
            f"application {application_id} is {row['state']!r} — it has been submitted, "
            f"and the stored resume is the record of what the employer received. "
            f"Rebuilding it would replace that with something never sent. Tailor "
            f"against the description on /tailor if you want to see what the current "
            f"prompt would produce."
        )
    if row["state"] not in BUILDABLE_STATES:
        raise TailorError(
            f"application {application_id} is {row['state']!r}; a packet is built from "
            f"{', '.join(sorted(BUILDABLE_STATES))}"
        )
    rebuild = row["state"] == states.PACKET_READY

    jd = (row["jd_text"] or "").strip()
    if not jd:
        # Rippling and BambooHR return no description at all. Say so plainly —
        # tailoring against an empty JD would select bullets at random and the
        # result would look like a real packet.
        raise NoJobDescription(
            f"no job description stored for application {application_id}. "
            f"Paste one on /tailor, or apply from the master resume."
        )

    # Before tailoring, not after: the point of knowing the posting wants AWS is
    # to get the nearest supported evidence *onto* the page, which is a selection
    # decision. Stored, so this is one call on the first build and free on every
    # rebuild and chat revision.
    from . import gaps as gaps_mod

    try:
        found_gaps = gaps_mod.analyse(
            conn, application_id, jd_text=jd, on_progress=on_progress
        )
        gap_block = gaps_mod.prompt_block(found_gaps)
    except (gaps_mod.GapError, llm.LLMError) as exc:
        # Never fatal. A resume without the adjacency hint is the resume this
        # system produced last week; no resume at all is a regression.
        log_gap_failure(application_id, exc)
        gap_block = ""

    # strict=False: always produce a document, and hand the objections back for a
    # human to act on. Every resume here is read and edited before it is sent, so
    # a rejection that renders nothing costs more than an annotation does.
    result = tailor(
        conn,
        jd,
        limit=10,
        application_id=application_id,
        strict=False,
        on_progress=on_progress,
        gap_block=gap_block,
    )
    return _store(
        conn, application_id, result, jd, rebuild=rebuild, on_progress=on_progress
    )


def _store(
    conn: sqlite3.Connection,
    application_id: int,
    result: TailorResult,
    jd: str,
    *,
    rebuild: bool,
    on_progress: ProgressFn | None = None,
    detail: str = "rebuilt",
) -> dict[str, Any]:
    """Render a checked selection and put it on the row. The only writer.

    Split out of `build_packet` so a revision accepted in chat lands through the
    identical path — same render, same findings column, same event — rather than
    becoming a second way for a resume to reach the database with its own subtly
    different rules.
    """
    # Deferred, not module-level: `resume` pulls in rendercv, which is the
    # optional `[resume]` extra. Importing it at the top would make `tailor`
    # unimportable — and so the whole dashboard unimportable — on an install
    # that only ever wanted to score.
    from pathlib import Path

    from . import resume

    report: ProgressFn = on_progress or (lambda **_: None)
    report(phase="rendering", message="typesetting the PDF")
    # The JD reaches the renderer too, not only the model: it decides which 22 of
    # ~50 technologies survive the Skills cut. Without it a keyword screen sees
    # 3Dmol.js where it should see React.
    document = resume.build_document(
        conn, selection=result.selection(), jd_text=jd, summary=result.summary
    )
    # Never a company name in the filename — CLAUDE.local.md, Discretion.
    out_path = config.OUT_DIR / f"packet_{application_id}.pdf"
    resume.render(document, Path(out_path))
    pdf_bytes = Path(out_path).read_bytes()

    flagged = f", {len(result.findings)} to review" if result.findings else ""
    summary = (
        f"packet: {len(result.bullets)} bullets, {len(pdf_bytes):,} bytes{flagged}"
    )
    with transaction(conn):
        conn.execute(
            "UPDATE applications SET resume_pdf = ?, resume_data = ?, "
            "resume_findings = ?, updated_at = ? WHERE id = ?",
            (
                pdf_bytes,
                json.dumps(document),
                json.dumps([f.as_dict() for f in result.findings]),
                config.utcnow(),
                application_id,
            ),
        )
        if rebuild:
            # Already `packet_ready`, so there is no transition to make — but an
            # overwritten resume must not be a silent one. The sha of the prompt
            # that produced it is in `llm_calls`; this is the row that says the
            # bytes changed at all, and when.
            states.log_event(
                conn,
                application_id,
                "note",
                detail=f"{detail} {summary}",
            )
        else:
            states.transition(
                conn, application_id, states.PACKET_READY, detail=summary
            )
    return {
        "application_id": application_id,
        "bullets": len(result.bullets),
        "reworded": sum(1 for b in result.bullets if b.changed),
        "bytes": len(pdf_bytes),
        "reasoning": result.reasoning,
    }


def apply_selection(
    conn: sqlite3.Connection,
    application_id: int,
    *,
    emitted: list[dict[str, Any]],
    summary: str,
    on_progress: ProgressFn | None = None,
    detail: str = "revised",
) -> dict[str, Any]:
    """Check and store a selection that came from somewhere other than a build.

    The checks are not skipped because a human asked for the wording: an
    instruction is a reason to *write* a line, never evidence that the corpus
    supports it. They run in the same non-blocking mode, so a revision that
    overreaches renders anyway and arrives annotated.
    """
    row = queries.packet_row(conn, application_id)
    if row is None:
        raise LookupError(f"no application {application_id}")
    jd = (row["jd_text"] or "").strip()

    found: list[Finding] = []
    bullets = validate(conn, emitted, findings=found)
    checked = validate_summary(conn, summary, findings=found)
    report: ProgressFn = on_progress or (lambda **_: None)
    report(phase="checking", message=f"reading {len(bullets)} lines back against source")
    try:
        review(conn, bullets, application_id=application_id, findings=found)
        review_summary(conn, checked, application_id=application_id, findings=found)
    except ReviewUnavailable as exc:
        found.append(
            Finding(
                kind="unchecked",
                where="selection",
                message=f"the claim checker did not run, so nothing here was read against its source: {exc}",
            )
        )
    result = TailorResult(bullets=bullets, summary=checked, findings=found)
    state = conn.execute(
        "SELECT state FROM applications WHERE id = ?", (application_id,)
    ).fetchone()["state"]
    return _store(
        conn,
        application_id,
        result,
        jd,
        rebuild=state == states.PACKET_READY,
        on_progress=on_progress,
        detail=detail,
    )


def build_pending(
    conn: sqlite3.Connection,
    limit: int | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, int]:
    """Build a packet for every `job_approved` row. What `make tailor` runs.

    Re-queries rather than working one snapshot, because approving a job is what
    starts this and approving the next one happens while it is still going. That
    second approval cannot start its own run — the lock is held, correctly — so
    if this drained only the list it began with, the row it missed would sit
    there until something else happened to run. The table is the queue.

    `attempted` is what stops the loop: a posting with no description fails the
    same way every time, and re-querying for `job_approved` would otherwise
    hand it back forever.
    """
    report: ProgressFn = on_progress or (lambda **_: None)
    counts = {"pending": 0, "built": 0, "no_jd": 0, "failed": 0}
    attempted: set[int] = set()

    while True:
        rows = conn.execute(
            "SELECT id FROM applications WHERE state = ? ORDER BY id",
            (states.JOB_APPROVED,),
        ).fetchall()
        queue = [int(r["id"]) for r in rows if int(r["id"]) not in attempted]
        if limit:
            queue = queue[: max(0, limit - len(attempted))]
        if not queue:
            break
        counts["pending"] = len(attempted) + len(queue)

        for application_id in queue:
            attempted.add(application_id)
            # `job_description` rather than `packet_row`: the latter carries the
            # apply URL and the stored resume, not the title and company this
            # needs to name what it is working on.
            row = queries.job_description(conn, application_id)
            report(
                phase="tailoring",
                message=f"{row['title']} · {row['company']}" if row else f"#{application_id}",
                done=len(attempted) - 1,
                total=counts["pending"],
                counts={k: v for k, v in counts.items() if k != "pending" and v},
            )
            try:
                build_packet(conn, application_id)
                counts["built"] += 1
            except NoJobDescription as exc:
                print(f"  - {exc}", file=sys.stderr)
                counts["no_jd"] += 1
            except (TailorError, llm.LLMError) as exc:
                # The row stays `job_approved`, so a later run retries it. One
                # bad posting must not stop the queue.
                print(f"  ! application {application_id}: {exc}", file=sys.stderr)
                counts["failed"] += 1

    report(
        phase="finished",
        message=f"{counts['built']} built",
        done=len(attempted),
        total=len(attempted),
        counts={k: v for k, v in counts.items() if k != "pending" and v},
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    """Build packets for the approved queue, or tailor against one JD file.

    With no argument this is the worker `docs/architecture.md` describes: every
    `job_approved` row gets a packet. `JD=path` keeps the single-posting path,
    which is how the prompt gets tuned without approving something first.
    """
    import argparse
    from pathlib import Path

    from .db import connect

    parser = argparse.ArgumentParser(description="Tailor the resume against one JD.")
    parser.add_argument(
        "jd",
        nargs="?",
        help="path to a file holding the job description, or - for stdin",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--pdf", default=None, help="also render a PDF here")
    parser.add_argument(
        "--metrics", action="store_true",
        help="list the metrics withheld from the prompt for counting artifacts",
    )
    args = parser.parse_args(argv)

    if args.metrics:
        conn = connect()
        try:
            weak = weak_metrics(conn)
            total = conn.execute(
                "SELECT count(*) FROM bullets WHERE metric IS NOT NULL AND metric != ''"
            ).fetchone()[0]
        finally:
            conn.close()
        print(f"{len(weak)} of {total} metrics withheld — they count what was produced")
        print("rather than what changed, so showing them recommends the wrong number.\n")
        for bullet_id, metric in weak:
            print(f"  bullet {bullet_id:>5}  {metric}")
        print("\nFix them in docs/profile/experience.yaml and re-run `make load-profile`,")
        print("or leave them: they stay in the corpus and the validator either way.")
        return 0

    # No JD given means the queue, which is the ordinary case.
    if not args.jd:
        conn = connect()
        try:
            counts = build_pending(conn, args.limit if args.limit != 10 else None)
        finally:
            conn.close()
        if not counts["pending"]:
            print("nothing approved — approve something on /review first")
            return 0
        print(" · ".join(f"{v} {k}" for k, v in counts.items()))
        return 1 if counts["failed"] else 0

    jd_text = sys.stdin.read() if args.jd == "-" else Path(args.jd).read_text()

    conn = connect()
    try:
        result = tailor(conn, jd_text, limit=args.limit)
    except FabricationError as exc:
        print(f"REJECTED — nothing was rendered.\n{exc}", file=sys.stderr)
        return 2
    except TailorError as exc:
        print(f"tailoring failed: {exc}", file=sys.stderr)
        return 1

    print(f"kept {len(result.bullets)} bullets · {result.reasoning}\n")
    for row in diff(result):
        if row["changed"]:
            print(f"[{row['bullet_id']}]\n  - {row['before']}\n  + {row['after']}")
        else:
            print(f"[{row['bullet_id']}] unchanged")

    if args.pdf:
        from . import resume

        path = resume.render(
            resume.build_document(
                conn, selection=result.selection(), jd_text=jd_text,
                summary=result.summary,
            ),
            Path(args.pdf),
        )
        print(f"\n{path}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
