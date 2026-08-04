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

# Shared credit reworded as sole ownership. `solo = 0` means the corpus says
# other people were involved.
_FIRST_PERSON_SINGULAR = re.compile(
    r"\b(?:i|my|myself|single-handedly|solely|by myself|on my own)\b", re.IGNORECASE
)
_SOLE_OWNERSHIP_VERBS = re.compile(
    r"^\s*(?:sole|single-handed|independently)\b", re.IGNORECASE
)
# Verbs that keep a shared bullet in team voice. Enumerating these works where
# enumerating ownership verbs does not: "owned", "led", "drove", "spearheaded",
# "headed", "architected" and the rest are an open set the model can always add
# to, but the ways English says "with other people" are few. So the rule is
# inverted — a shared bullet must stay in one of these frames rather than avoid
# a list of forbidden ones.
_TEAM_VOICE = frozenset(
    """assisted collaborated contributed cooperated coordinated helped joined
    paired participated partnered supported worked""".split()
)
# Words that name the other people, and so keep shared work visible even when
# the opening verb is the source's own.
_SHARED_MARKERS = re.compile(
    r"\b(?:we|our|us|team|teams|teammate\w*|colleague\w*|coworker\w*|engineers|"
    r"developers|peers|group|squad|alongside|jointly|collaborat\w*|contribut\w*|"
    r"cross-functional|partner(?:ed|ship)?|co-(?:led|owned|authored|wrote|built|"
    r"developed|designed))\b",
    re.IGNORECASE,
)

# Scope widened with ordinary words — "the order service" becoming "the entire
# company platform" introduces no proper noun and no number, so nothing else
# here sees it. This is a closed set: English has few ways to say "all of it",
# unlike the unbounded set of names a model can invent. `full`, `complete` and
# `global` are left out on purpose — "full-stack", "full test coverage" and
# "global state" are ordinary phrasings and would only generate false alarms.
_SCOPE_MAGNIFIERS = re.compile(
    r"\b(?:entire|entirely|whole|every|everything|everyone|all|"
    r"company-?wide|org(?:anization)?-?wide|enterprise-?wide|team-?wide|"
    r"site-?wide|platform-?wide|fleet-?wide|firm-?wide|worldwide|nationwide|"
    r"across (?:the )?(?:company|organization|org|business|enterprise))\b",
    re.IGNORECASE,
)


class TailorError(RuntimeError):
    """The model's output is unusable."""


class FabricationError(TailorError):
    """The model asserted something the corpus does not support.

    This is the failure the whole design exists to catch. It is never downgraded
    to a warning and never rendered anyway.
    """


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

    def selection(self) -> dict[int, str]:
        """The shape `resume.build_cv` wants: bullet id -> final text, in order."""
        return {b.bullet_id: b.text for b in self.bullets}


# ================================== prompting ==================================

_SYSTEM = """You tailor one candidate's resume to one job description.

You will be given numbered source bullets. Each is a factual record of something \
the candidate actually did. Your job is to choose the most relevant ones and \
sharpen their wording for this specific job.

Lines beginning with `#` describe the role or project the bullets under them \
belong to — scope, role, how much real use it saw. Use them to judge which \
bullets are worth showing. They are background, not material to quote.

You MAY: select a subset, reorder, reword, change emphasis, foreground different \
skills, tighten for length.

You MAY NOT, under any circumstance:
- write a bullet that is not derived from exactly one source bullet
- introduce an employer, job title, date, degree, certification, or metric that \
is not in that bullet's source text
- change any number, in either direction, or add one the source lacks — this \
includes team sizes, years of experience, and percentages
- describe work marked `shared: true` as something you did alone, or use \
first-person-singular ownership language for it
- merge two bullets into one

Start every bullet with a verb. Never open one with a company, product, or \
tool name — the validator reads a capitalized first word as a proper-noun \
claim and rejects the result. Write technology names with their normal \
capitalization (Redis, Kubernetes, Postgres), never lowercased.

Do not widen scope. If the source says one service, do not write "the entire \
platform"; if it describes work shared with others, keep the other people \
visible rather than reworking it into something you led alone.

A downstream validator checks every one of these mechanically and rejects the \
whole result on any violation. A rejected result is worse than a conservative one.

Return ONLY a JSON object, no prose around it:
{"reasoning": "one or two sentences on what you optimized for",
 "bullets": [{"id": <source bullet id>, "text": "<final wording>"}]}

Order the `bullets` array the way they should appear on the resume."""


def build_prompt(
    conn: sqlite3.Connection, jd_text: str, *, limit: int | None = None
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
        if row["metric"]:
            flags.append(f"metric: {row['metric']}")
        flags.append(f"shared: {'false' if row['solo'] else 'true'}")
        lines.append(f"[{row['id']}] ({where}) [{'; '.join(flags)}]")
        lines.append(f"    {row['text']}")
    corpus = "\n".join(lines)

    ask = (
        "JOB DESCRIPTION\n\n"
        f"{jd_text.strip()}\n\n"
        "Select the bullets that best evidence fitness for this role"
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


def parse_response(raw: str) -> tuple[list[dict[str, Any]], str]:
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
    return payload["bullets"], str(payload.get("reasoning", "")).strip()


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
    """Number words, as their digit form, so `forty` and `40` compare equal."""
    return {
        _NUMBER_WORDS[word.lower()]
        for word in _WORD.findall(text)
        if word.lower() in _NUMBER_WORDS
    }


# A word, for proper-noun purposes. `+` and `#` stay so `C++` and `C#` survive;
# `-` does not, so `PhD-level` yields `PhD` rather than hiding it in a compound.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+.#']*")
# Capitalization carries no claim at the start of a sentence or clause.
_CLAUSE_START = re.compile(r"(?<=[.!?:;])\s+")

# Verbs a bullet may open with even though the source row words it differently.
# Only needed for forms `_stem` cannot relate to the source — mostly irregular
# past tenses, where "drove" shares no prefix with "drive". Add to it freely: a
# missing verb costs a rejection with a message naming the word, never a
# fabrication getting through.
_BULLET_OPENERS = frozenset(
    """
    achieved added advocated analyzed architected audited authored automated
    benchmarked broke brought built centralized chose coached collaborated
    consolidated contributed converted coordinated created cut debugged decreased
    delivered deployed designed developed diagnosed documented doubled drafted drove
    eliminated enabled ensured established evaluated expanded extended facilitated
    fixed formalized found generated grew guided halved handled hardened headed
    helped held identified implemented improved increased informed initiated
    instrumented integrated introduced investigated joined launched led maintained
    made managed mentored migrated modeled modernized monitored moved negotiated
    onboarded operated optimized organized overhauled oversaw owned partnered
    piloted planned ported prevented prioritized produced profiled proposed proved
    provided prototyped published ran rearchitected rebuilt reduced refactored
    refined removed replaced reported researched resolved restructured retired
    reviewed revised rewrote rolled saved scaled scoped secured served set shaped
    shipped shrank simplified solved sourced sped split spun stabilized staffed
    standardized started streamlined strengthened supported surfaced sustained
    taught tested tightened took tracked trained transformed translated trimmed
    tripled tuned turned unblocked unified upgraded validated verified wired won
    worked wrote
    """.split()
)

# Suffixes stripped to relate a reworded verb to its source form: the output's
# "Replaced" and the source's "replacing" both reduce to "replac".
_INFLECTIONS = ("ing", "ed", "es", "s", "d")


def _stem(word: str) -> str:
    """Crude suffix strip, enough to match inflections of the same source verb."""
    lowered = word.lower()
    for suffix in _INFLECTIONS:
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 3:
            return lowered[: -len(suffix)]
    return lowered


_TECH_COLUMNS = ("tech_built", "tech_owned", "tech_maintained", "tech_touched")


def _corpus_names(conn: sqlite3.Connection) -> set[str]:
    """Every employer, project and technology the corpus knows about, lowercased.

    Two filters, both load-bearing. Only the structured fields are read —
    company, title, project name, the `tech_*` lists, bullet skills — because
    the free-prose columns (`company_context`, `scope`, `role`) are full of
    ordinary words. And within those, only tokens written as proper nouns
    count: a skill recorded as "row-level security" or "client delivery"
    contributes nothing, while "React Router" and "Kubernetes" do.

    Without the second filter this set fills with words like "security" and
    "production", and every bullet that reworded its way into one of them would
    be rejected for borrowing a name from another role.
    """
    names: set[str] = set()

    def add(value: str) -> None:
        for word in _WORD.findall(value):
            token = word.rstrip(".")
            if token and (token[0].isupper() or any(c.isdigit() for c in token)):
                names.add(token.lower())

    def collect(row: sqlite3.Row, *plain: str) -> None:
        columns = row.keys()
        for name in plain:
            if name in columns and row[name]:
                add(str(row[name]))
        for column in (*_TECH_COLUMNS, "skills"):
            if column in columns and row[column]:
                try:
                    items = json.loads(row[column])
                except json.JSONDecodeError:
                    continue
                for item in items:
                    add(str(item))

    for row in queries.corpus_experiences(conn):
        collect(row, "company", "title")
    for row in queries.corpus_projects(conn):
        collect(row, "name")
    for row in queries.corpus_bullets(conn):
        collect(row)
    return names


def _unsourced_proper_nouns(
    text: str, haystack: str, known_names: frozenset[str] | set[str] = frozenset()
) -> list[str]:
    """Capitalized words in the output that its source row never mentions.

    This is the check that catches an invented employer or a fabricated degree.
    A list of known-bad names cannot work — the model invents names we have never
    heard of, so "Stripe" and "PhD" match nothing. Inverting it does work: on a
    resume, a capitalized word is a proper noun, and every proper noun has to
    come from the bullet it claims to describe.

    The hard case is a word that opens a clause, where English capitalizes
    regardless. Skipping those outright — which this used to do — meant
    "At Stripe, cut p99 latency" was caught but "Stripe checkout latency cut
    from 840ms" was not, and a semicolon bought the same bypass mid-bullet. So
    clause-initial words are checked too, against three escapes that a real
    proper noun does not satisfy: the source names it, the source names an
    inflection of it, or it is an ordinary bullet-opening verb.
    """
    # `.` is allowed inside a word so `U.S.` survives, which means a word ending a
    # sentence arrives as `Postgres.` — strip it, or every final word looks new.
    words = [word.lower().rstrip(".") for word in _WORD.findall(haystack)]
    sourced = set(words)
    sourced_stems = {_stem(word) for word in words}

    found: list[str] = []
    for clause in _CLAUSE_START.split(text):
        for position, match in enumerate(_WORD.finditer(clause)):
            token = match.group(0).rstrip(".")
            if not token:
                continue
            # Capitalization is the usual tell for a proper noun, but a
            # lowercase `kubernetes` claims a technology just as loudly. Those
            # get checked too when the corpus knows the name from somewhere —
            # which is what catches a tool borrowed from a different job.
            if not token[0].isupper() and token.lower() not in known_names:
                continue
            if token.lower() in sourced:
                continue
            if position == 0:
                # Capitalization here is grammar, so demand less: a rewording of
                # a source word, or a verb any bullet could start with.
                if _stem(token) in sourced_stems or token.lower() in _BULLET_OPENERS:
                    continue
            found.append(token)
    return found


def validate(
    conn: sqlite3.Connection, emitted: list[dict[str, Any]]
) -> list[TailoredBullet]:
    """Check every rule in .claude/rules/tailoring.md. Raise on the first failure.

    Returns the accepted bullets. Never returns partial results — a resume that
    is 90% sourced is not 90% acceptable.
    """
    if not emitted:
        raise TailorError("the model selected no bullets")

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
        raise FabricationError(
            f"the model emitted bullet ids that are not in the corpus: {missing}. "
            "Every bullet on the resume must come from a `bullets` row."
        )
    noun_context = _noun_context(conn)
    known_names = _corpus_names(conn)
    duplicates = {bid for bid in ids if ids.count(bid) > 1}
    if duplicates:
        raise FabricationError(
            f"bullet ids used more than once: {sorted(duplicates)}. "
            "One source row produces at most one resume line."
        )

    accepted: list[TailoredBullet] = []
    for item, bid in zip(emitted, ids):
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
            raise FabricationError(
                f"bullet {bid} contains number(s) {sorted(invented)} that do not appear "
                f"in its source row. Source: {source['text']!r}"
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
            raise FabricationError(
                f"bullet {bid} uses identifier(s) {invented_tokens} that do not appear "
                f"in its source row. A digit inside a name is still a claim — `p95` is "
                f"not `p99` and `v4` is not `v1`. Source: {source['text']!r}"
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
            raise FabricationError(
                f"bullet {bid} contains letter(s) {foreign} that appear nowhere in its "
                f"source row — a homoglyph reads as an ordinary word to every check "
                f"here. Source: {source['text']!r}"
            )

        # 3 and 4. Employers, titles, dates, degrees and schools are structural:
        # they come from `experiences` and `education`, never from bullet text.
        # Any proper noun the source row does not contain is an invention.
        invented_names = _unsourced_proper_nouns(text, context, known_names)
        if invented_names:
            raise FabricationError(
                f"bullet {bid} mentions {invented_names}, which do not appear in its "
                f"source row. Employers, schools, degrees and product names come from "
                f"the corpus, not from the tailored wording. Source: {source['text']!r}"
            )

        # 5. Shared credit must not become sole ownership.
        if not source["solo"]:
            if _FIRST_PERSON_SINGULAR.search(text) or _SOLE_OWNERSHIP_VERBS.search(text):
                raise FabricationError(
                    f"bullet {bid} is shared credit (solo = 0) but the tailored wording "
                    f"claims individual ownership: {text!r}"
                )
            # The pronoun check above only catches the blatant form. "Led the
            # migration" and "Owned and executed the migration" claim sole
            # credit just as squarely without using a banned word, so a shared
            # bullet must positively stay in a shared frame: keep the source's
            # own verb, open in team voice, or name the other people.
            opener = next(iter(_WORD.findall(text)), "")
            if (
                opener.lower() not in _TEAM_VOICE
                and _stem(opener) not in {_stem(w) for w in _WORD.findall(haystack)}
                and not _SHARED_MARKERS.search(text)
            ):
                raise FabricationError(
                    f"bullet {bid} is shared credit (solo = 0) but the tailored wording "
                    f"reads as sole ownership: it opens on {opener!r}, which is neither "
                    f"the source's own verb nor a team-voice one, and nothing in it "
                    f"names the other people involved. Keep the source's verb, open "
                    f"with one of {sorted(_TEAM_VOICE)[:4]}..., or say who else was "
                    f"there. Text: {text!r}"
                )

        # 6. Scope must not be widened with ordinary words. Introduces no proper
        #    noun and no number, so nothing above catches it.
        stretched = {m.group(0).lower() for m in _SCOPE_MAGNIFIERS.finditer(text)}
        stretched -= {m.group(0).lower() for m in _SCOPE_MAGNIFIERS.finditer(haystack)}
        if stretched:
            raise FabricationError(
                f"bullet {bid} widens its scope with {sorted(stretched)}, which the "
                f"source row does not claim. Source: {source['text']!r}"
            )

        accepted.append(
            TailoredBullet(
                bullet_id=bid,
                text=text,
                source_text=source["text"],
                changed=text.strip() != source["text"].strip(),
            )
        )
    return accepted


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
    tech_columns = ("tech_built", "tech_owned", "tech_maintained", "tech_touched")

    def words(row: sqlite3.Row, *extra: str) -> str:
        parts = [str(row[name]) for name in extra if name in row.keys() and row[name]]
        for column in tech_columns:
            if column in row.keys() and row[column]:
                try:
                    parts.extend(str(item) for item in json.loads(row[column]))
                except json.JSONDecodeError:
                    pass
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

    context: dict[int, str] = {}
    for row in queries.corpus_bullets(conn):
        parent = (
            experience_context.get(int(row["experience_id"]), "")
            if row["experience_id"] is not None
            else project_context.get(int(row["project_id"]), "")
        )
        own_skills = ""
        if row["skills"]:
            try:
                own_skills = " ".join(str(item) for item in json.loads(row["skills"]))
            except json.JSONDecodeError:
                pass
        context[int(row["id"])] = f"{parent} {own_skills}"
    return context


# =================================== driving ===================================


def tailor(
    conn: sqlite3.Connection,
    jd_text: str,
    *,
    limit: int | None = 12,
    application_id: int | None = None,
    expect_repeat: bool = True,
) -> TailorResult:
    """One tailoring pass: prompt, parse, validate. Raises unless all three hold.

    `expect_repeat` passes straight to `llm.complete` — leave it True when
    working through a queue, set it False for a single hand-pasted JD.
    """
    if not jd_text.strip():
        raise TailorError("no job description to tailor against")
    corpus, prompt = build_prompt(conn, jd_text, limit=limit)
    raw = llm.complete(
        "tailor",
        prompt,
        conn=conn,
        system=_SYSTEM,
        cached=corpus,
        application_id=application_id,
        expect_repeat=expect_repeat,
    )
    emitted, reasoning = parse_response(raw)
    return TailorResult(bullets=validate(conn, emitted), reasoning=reasoning)


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


def build_packet(conn: sqlite3.Connection, application_id: int) -> dict[str, Any]:
    """`job_approved` -> tailored PDF stored on the row -> `packet_ready`.

    The PDF bytes and the RenderCV input that produced them are written here and
    never rewritten. `.claude/rules/data-layer.md` calls `resume_pdf` the record
    of what was actually sent, and freezing it at build time is what makes that
    true: a packet built today and submitted next week carries the resume I
    actually downloaded, not whatever the templates render by then.

    Rendering happens before the transition and inside the same transaction as
    it, so a failed render leaves the row in `job_approved` to retry rather than
    in `packet_ready` with nothing attached.
    """
    from pathlib import Path

    from . import resume

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
    if row["state"] != states.JOB_APPROVED:
        raise TailorError(
            f"application {application_id} is {row['state']!r}; a packet is built "
            f"from {states.JOB_APPROVED!r}"
        )

    jd = (row["jd_text"] or "").strip()
    if not jd:
        # Rippling and BambooHR return no description at all. Say so plainly —
        # tailoring against an empty JD would select bullets at random and the
        # result would look like a real packet.
        raise NoJobDescription(
            f"no job description stored for application {application_id}. "
            f"Paste one on /tailor, or apply from the master resume."
        )

    result = tailor(conn, jd, limit=10, application_id=application_id)
    document = resume.build_document(conn, selection=result.selection())
    # Never a company name in the filename — CLAUDE.local.md, Discretion.
    out_path = config.OUT_DIR / f"packet_{application_id}.pdf"
    resume.render(document, Path(out_path))
    pdf_bytes = Path(out_path).read_bytes()

    with transaction(conn):
        conn.execute(
            "UPDATE applications SET resume_pdf = ?, resume_data = ?, updated_at = ? "
            "WHERE id = ?",
            (pdf_bytes, json.dumps(document), config.utcnow(), application_id),
        )
        states.transition(
            conn,
            application_id,
            states.PACKET_READY,
            detail=f"packet: {len(result.bullets)} bullets, {len(pdf_bytes):,} bytes",
        )
    return {
        "application_id": application_id,
        "bullets": len(result.bullets),
        "reworded": sum(1 for b in result.bullets if b.changed),
        "bytes": len(pdf_bytes),
        "reasoning": result.reasoning,
    }


def build_pending(conn: sqlite3.Connection, limit: int | None = None) -> dict[str, int]:
    """Build a packet for every `job_approved` row. What `make tailor` runs."""
    rows = conn.execute(
        "SELECT id FROM applications WHERE state = ? ORDER BY id"
        + (f" LIMIT {int(limit)}" if limit else ""),
        (states.JOB_APPROVED,),
    ).fetchall()
    counts = {"pending": len(rows), "built": 0, "no_jd": 0, "failed": 0}
    for row in rows:
        try:
            build_packet(conn, int(row["id"]))
            counts["built"] += 1
        except NoJobDescription as exc:
            print(f"  - {exc}", file=sys.stderr)
            counts["no_jd"] += 1
        except (TailorError, llm.LLMError) as exc:
            # The row stays `job_approved`, so a rerun retries it. One bad
            # posting must not stop the queue.
            print(f"  ! application {row['id']}: {exc}", file=sys.stderr)
            counts["failed"] += 1
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
    args = parser.parse_args(argv)

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
            resume.build_document(conn, selection=result.selection()), Path(args.pdf)
        )
        print(f"\n{path}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
