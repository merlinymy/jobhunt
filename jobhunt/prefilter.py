"""Pass 1 of scoring: the deterministic filter, driven by docs/profile/scoring.yaml.

Separate from `score.py` because it is the half that costs nothing and can be
reasoned about. It runs over every `discovered` row, rejects what I would
genuinely refuse, and hands the rest to the LLM pass. Getting this wrong is
expensive in the direction nobody notices: a posting dropped here never reaches
the digest and leaves no trace beyond a `filtered` row.

Two rules from CLAUDE.md shape every decision in this file.

**Filters drop roles I would genuinely refuse, never shots on unreliable or
missing data.** So every check here fails *open*. A title with no level marker
passes the seniority band. A posting with no JD passes the tech dealbreakers. A
company we cannot name passes the blocklist. Absent evidence is never treated as
disqualifying evidence, and that asymmetry is deliberate everywhere below.

**Title and level are the only hard filters. Location ranks, comp does neither.**
Nothing in this module looks at compensation, and location appears only as an
ordering key. If a rule here starts rejecting on either, it is a bug.

Every rejection records *which* rule fired, in the `events` row, so a digest
that has gone quiet can be traced to the rule that did it rather than guessed at.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import config, states
from .db import transaction
from .runs import ProgressFn

SCORING_YAML = config.PROFILE_DIR / "scoring.yaml"


class PrefilterError(RuntimeError):
    """scoring.yaml is unreadable or the wrong shape."""


# Level markers, most specific first — `senior staff` must not read as `senior`.
# Only what a title *states* counts. The overwhelming majority of titles carry no
# marker at all, and those are `None`, which passes.
_LEVEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("above", re.compile(
        r"\b(?:staff|principal|distinguished|fellow|architect|director|"
        r"head\s+of|vp|vice\s+president|chief|cto|manager|mgr)\b", re.I)),
    ("senior", re.compile(r"\b(?:senior|sr\.?|lead|iii|3)\b", re.I)),
    ("mid", re.compile(r"\b(?:mid|intermediate|ii|2)\b", re.I)),
    ("entry", re.compile(
        r"\b(?:junior|jr\.?|entry|associate|new\s+grad|newgrad|graduate|"
        r"university|campus|early\s+career|apprentice|i|1)\b", re.I)),
)

# Phrases that contain a level word but do not state a level. Stripped before
# anything above is matched.
#
# "Member of Technical Staff" is the whole reason this exists. It is the
# ordinary IC title at OpenAI, Anthropic and xAI — scoring.yaml's own comment
# flags it as the variant no keyword catches — and matching `staff` inside it
# read it as staff-level and rejected precisely the roles worth the most.
# "Architect" is here for the same reason in reverse: "Solutions Architect" is
# a level, but "architecture" in a title is usually the domain.
_NOT_A_LEVEL = re.compile(
    r"\b(?:member\s+of\s+(?:the\s+)?technical\s+staff|technical\s+staff|mts)\b", re.I
)

# `junior` and `entry` are separate words in scoring.yaml but the same rung, and
# a config that lists one and not the other should not reject the other.
_LEVEL_ALIASES = {"junior": "entry", "entry": "entry", "mid": "mid", "senior": "senior"}


def detect_level(title: str) -> str | None:
    """The level a title states, or None when it states none.

    None is the common case and always passes. Guessing a level from a title
    that does not give one is exactly the "reject on missing data" this file
    exists to avoid.
    """
    cleaned = _NOT_A_LEVEL.sub(" ", title)
    for level, pattern in _LEVEL_PATTERNS:
        if pattern.search(cleaned):
            return level
    return None


@dataclass
class Config:
    """scoring.yaml, parsed once. Empty lists mean "this rule is off"."""

    title_keywords: list[str] = field(default_factory=list)
    titles_block: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    companies_block: list[str] = field(default_factory=list)
    tech_required: list[str] = field(default_factory=list)
    tech_dealbreakers: list[str] = field(default_factory=list)
    exclusions: dict[str, bool] = field(default_factory=dict)
    location_rank: list[str] = field(default_factory=list)


def _lower_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def load() -> Config:
    if not SCORING_YAML.exists():
        raise PrefilterError(f"{SCORING_YAML} is missing. See docs/intake.md.")
    try:
        raw = yaml.safe_load(SCORING_YAML.read_text()) or {}
    except yaml.YAMLError as exc:
        raise PrefilterError(f"scoring.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PrefilterError("scoring.yaml: expected a mapping at the top level")
    exclusions = raw.get("exclusions")
    return Config(
        title_keywords=_lower_list(raw.get("title_keywords")),
        titles_block=_lower_list(raw.get("titles_block")),
        seniority=_lower_list(raw.get("seniority")),
        companies_block=_lower_list(raw.get("companies_block")),
        tech_required=_lower_list(raw.get("tech_required")),
        tech_dealbreakers=_lower_list(raw.get("tech_dealbreakers")),
        exclusions={k: bool(v) for k, v in (exclusions or {}).items()}
        if isinstance(exclusions, dict)
        else {},
        location_rank=_lower_list(raw.get("location_rank")),
    )


# ================================== exclusions ==================================
#
# Phrase sets for the `exclusions:` block. These read the JD, so each one is
# skipped entirely when there is no JD — several board vendors return none, and
# "no description" must not become "rejected".

_EXCLUSION_PATTERNS: dict[str, re.Pattern[str]] = {
    "clearance_required": re.compile(
        r"\b(?:active\s+)?(?:ts/sci|top\s+secret|secret\s+clearance|security\s+clearance|"
        r"dod\s+clearance|polygraph|public\s+trust\s+clearance)\b", re.I),
    "unpaid": re.compile(
        r"\b(?:unpaid|no\s+compensation|volunteer\s+(?:position|role|basis)|"
        r"equity[\s-]only|for\s+exposure)\b", re.I),
    "commission_only": re.compile(
        r"\b(?:commission[\s-]only|100%\s+commission|straight\s+commission|"
        r"uncapped\s+commission\s+only)\b", re.I),
    "heavy_oncall": re.compile(
        r"\b(?:24/7\s+on[\s-]call|rotating\s+on[\s-]call|pager\s+duty\s+rotation|"
        r"on[\s-]call\s+rotation)\b", re.I),
}

# Staffing agencies are matched on the company, not the JD — the tell is the
# employer, and their descriptions read like anyone else's.
_STAFFING = re.compile(
    r"\b(?:staffing|recruit(?:ing|ers?|ment)|talent\s+(?:solutions|acquisition|"
    r"partners|group)|consultanc(?:y|ies)|resourc(?:es|ing)\s+group|manpower|"
    r"headhunt\w*|placement\s+services|technologies\s+staffing)\b", re.I)


@dataclass
class Verdict:
    """Why a posting was kept or dropped. `rule` is None when it passed."""

    passed: bool
    rule: str | None = None
    detail: str | None = None

    def as_event(self) -> str:
        return f"prefilter: {self.rule} — {self.detail}" if self.rule else "prefilter: passed"


def evaluate(job: sqlite3.Row, cfg: Config, company: str | None = None) -> Verdict:
    """Apply every rule to one posting. First rejection wins.

    Ordered cheapest and most decisive first: title, then level, then company,
    then anything that has to read the JD.
    """
    title = (job["title"] or "").strip()
    if not title:
        # Cannot judge it, so do not reject it. `store` already refuses rows
        # with no title, so this is defensive rather than expected.
        return Verdict(True)
    lowered = title.lower()

    # 1. Title blocklist. The short, enumerable, genuinely-refused side.
    for word in cfg.titles_block:
        if word in lowered:
            return Verdict(False, "titles_block", f"title contains {word!r}")

    # 2. Title keywords. Wide on purpose: an exact-match allowlist silently
    #    drops variants nobody enumerated, and a dropped posting is a lost shot.
    if cfg.title_keywords and not any(word in lowered for word in cfg.title_keywords):
        return Verdict(False, "title_keywords", "no title keyword matched")

    # 3. Seniority. Only fires on a level the title actually states.
    if cfg.seniority:
        level = detect_level(title)
        allowed = {_LEVEL_ALIASES.get(item, item) for item in cfg.seniority}
        if level is not None and _LEVEL_ALIASES.get(level, level) not in allowed:
            return Verdict(False, "seniority", f"title reads as {level!r}")

    # 4. Company blocklist — competitors, past employers, hard nos.
    if company and cfg.companies_block:
        company_lower = company.lower()
        for name in cfg.companies_block:
            if name in company_lower:
                return Verdict(False, "companies_block", f"company matches {name!r}")

    if company and cfg.exclusions.get("staffing_agencies") and _STAFFING.search(company):
        return Verdict(False, "staffing_agencies", "company name reads as an agency")

    # Everything below reads the JD. No JD means these rules cannot fire —
    # several board vendors return none, and silence is not evidence.
    jd = (job["jd_text"] or "").strip()
    if not jd:
        return Verdict(True)

    for name, pattern in _EXCLUSION_PATTERNS.items():
        if cfg.exclusions.get(name):
            found = pattern.search(jd)
            if found:
                return Verdict(False, name, f"JD mentions {found.group(0)!r}")

    jd_lower = jd.lower()
    for tech in cfg.tech_dealbreakers:
        if tech in jd_lower:
            return Verdict(False, "tech_dealbreakers", f"JD mentions {tech!r}")

    if cfg.tech_required and not any(tech in jd_lower for tech in cfg.tech_required):
        return Verdict(False, "tech_required", "JD names none of the required tech")

    return Verdict(True)


# ================================== location ==================================
#
# Location RANKS and never rejects — the list in scoring.yaml ends in a
# catch-all. This produces the tier index the digest sorts on, nothing more.

_BAY_AREA = re.compile(
    r"\b(?:san\s+francisco|sf|oakland|berkeley|san\s+jose|palo\s+alto|mountain\s+view|"
    r"sunnyvale|santa\s+clara|cupertino|menlo\s+park|redwood\s+city|fremont|"
    r"south\s+san\s+francisco|emeryville|bay\s+area|silicon\s+valley)\b", re.I)
_CALIFORNIA = re.compile(r"(?:\bcalifornia\b|,\s*ca\b|\bca\s+\d{5})", re.I)
_METRO_PATTERNS: dict[str, re.Pattern[str]] = {
    # The state abbreviation is anchored on the comma that precedes it, as in
    # every other metro below. It was a bare `\bma\b`, which matches a standalone
    # "ma" anywhere in a location string — and `'` is not a word character, so
    # "Ma'anshan" read as Boston. Location only ranks and never rejects, so the
    # cost was a mis-ordered review queue rather than a lost posting, but a rule
    # that fires on the wrong continent is not ranking anything. The trailing
    # `.*` went with it: `search` already scans, so it matched nothing extra.
    "boston": re.compile(r"\b(?:boston|cambridge|somerville|waltham|burlington)\b|"
                         r",\s*ma\b", re.I),
    "chicago": re.compile(r"\b(?:chicago|evanston|schaumburg)\b|,\s*il\b", re.I),
    "atlanta": re.compile(r"\batlanta\b|,\s*ga\b", re.I),
    "seattle": re.compile(r"\b(?:seattle|bellevue|redmond|kirkland|tacoma)\b|,\s*wa\b", re.I),
}


def location_tier(job: sqlite3.Row, cfg: Config) -> int:
    """Index into `location_rank`. Lower sorts first; unknown lands on the catch-all.

    Never returns a "reject" value. There isn't one — scoring.yaml's list ends in
    `anywhere` precisely so that nothing is dropped for where it is.
    """
    order = cfg.location_rank or ["anywhere"]

    def rank(name: str) -> int:
        return order.index(name) if name in order else len(order)

    if (job["remote"] or "").lower() == "remote":
        return rank("remote")
    where = (job["location"] or "")
    if _BAY_AREA.search(where):
        return rank("bay-area")
    if _CALIFORNIA.search(where):
        return rank("california")
    for name, pattern in _METRO_PATTERNS.items():
        if pattern.search(where):
            return rank(name)
    return rank("anywhere")


# =================================== running ===================================


def run(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, int]:
    """Walk every `discovered` application. Returns counts by outcome.

    Only rejections move. A survivor stays `discovered` and is picked up by the
    LLM pass, which is what actually earns it a `scored` row — moving it here
    would leave `applications.score` NULL on a state whose whole meaning is that
    a score exists. So after this runs, every remaining `discovered` row is one
    that passed, and `score.py` needs no flag to find them.

    Each posting is its own transaction. A pass over five thousand rows that dies
    on row 4,000 keeps the 3,999 decisions already made — they are deterministic,
    and rerunning reaches the same verdict for whatever is left.
    """
    cfg = load()
    counts = {"examined": 0, "passed": 0, "filtered": 0}
    by_rule: dict[str, int] = {}

    sql = """
        SELECT a.id AS application_id, j.company_id, j.title_norm,
               j.title, j.jd_text, j.location, j.remote,
               c.name AS company
          FROM applications a
          JOIN jobs j      ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.state = ?
         ORDER BY a.id
    """
    fetched = conn.execute(
        sql + (f" LIMIT {int(limit)}" if limit else ""), (states.DISCOVERED,)
    ).fetchall()
    # Roles already handled at another location, computed once for the pass: a
    # duplicate city of one is dropped below before the LLM ever scores it.
    from . import queries

    handled = queries.handled_role_keys(conn)
    for index, row in enumerate(fetched):
        # Every 25, and outside the transaction below. Free and deterministic,
        # but a backlog of several thousand is still ten seconds of a dashboard
        # saying nothing at all.
        if on_progress and index % 25 == 0:
            on_progress(
                phase="prefilter", done=index, total=len(fetched),
                counts={k: v for k, v in counts.items() if v},
            )
        counts["examined"] += 1
        key = (int(row["company_id"]), row["title_norm"] or row["title"])
        if key in handled:
            # Same role already applied to, skipped, rejected, or in a packet at
            # another location — one shot, already taken or set aside. Dropped
            # here so a duplicate city is never scored.
            verdict = Verdict(False, "duplicate", "same role already handled elsewhere")
        else:
            verdict = evaluate(row, cfg, row["company"])
        if verdict.passed:
            counts["passed"] += 1
            continue
        with transaction(conn):
            states.transition(
                conn,
                int(row["application_id"]),
                states.FILTERED,
                detail=verdict.as_event(),
            )
        counts["filtered"] += 1
        by_rule[verdict.rule or "?"] = by_rule.get(verdict.rule or "?", 0) + 1
    counts.update({f"rule:{name}": n for name, n in sorted(by_rule.items())})
    return counts


def recheck(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, int]:
    """Re-apply the current rules to rows already `scored`. Returns counts by outcome.

    Editing `scoring.yaml` is what makes the review queue stale. `run()` only ever
    walks `discovered`, so a narrowed rule changes what arrives tomorrow and does
    nothing about the queue in front of you — narrowing the seniority band left
    1,652 of 4,161 cards asking about roles the rules had just refused, with no
    way to retire them but by hand, one at a time.

    Every rule is re-applied, not only the one that changed, because the question
    this answers is "does this posting still pass?" and there is only one honest
    way to ask it. That means a recheck can also drop rows on a rule that was
    already there — a JD-reading rule that could not fire when the posting was
    scored without a description, say. `dry_run` exists so that is visible as a
    breakdown before anything moves, and the counts name the rule either way.

    `filtered`, not `skipped`. Nobody read these; a rule rejected them, and the
    stats page counts the two differently.

    Deliberately not automatic. Re-running the rules over work already paid for
    is a decision about a queue, and `filtered` is terminal.
    """
    cfg = load()
    counts = {"examined": 0, "passed": 0, "filtered": 0}
    by_rule: dict[str, int] = {}

    sql = """
        SELECT a.id AS application_id, j.company_id, j.title_norm,
               j.title, j.jd_text, j.location, j.remote,
               c.name AS company
          FROM applications a
          JOIN jobs j      ON j.id = a.job_id
          JOIN companies c ON c.id = j.company_id
         WHERE a.state = ?
         ORDER BY a.id
    """
    fetched = conn.execute(
        sql + (f" LIMIT {int(limit)}" if limit else ""), (states.SCORED,)
    ).fetchall()
    # A scored row whose role has since been handled at another location is a
    # duplicate too — retire it here on a recheck, the same as the forward pass.
    from . import queries

    handled = queries.handled_role_keys(conn)

    for index, row in enumerate(fetched):
        if on_progress and index % 25 == 0:
            on_progress(
                phase="prefilter", done=index, total=len(fetched),
                counts={k: v for k, v in counts.items() if v},
            )
        counts["examined"] += 1
        key = (int(row["company_id"]), row["title_norm"] or row["title"])
        if key in handled:
            verdict = Verdict(False, "duplicate", "same role already handled elsewhere")
        else:
            verdict = evaluate(row, cfg, row["company"])
        if verdict.passed:
            counts["passed"] += 1
            continue
        counts["filtered"] += 1
        by_rule[verdict.rule or "?"] = by_rule.get(verdict.rule or "?", 0) + 1
        if dry_run:
            continue
        # Per row, as in `run()`: a pass over thousands that dies partway keeps
        # the decisions already made, and they are deterministic, so a rerun
        # reaches the same verdict for whatever is left.
        with transaction(conn):
            states.transition(
                conn,
                int(row["application_id"]),
                states.FILTERED,
                # Says it was a rules change rather than a first-pass rejection,
                # so the history does not read as though this was filtered at
                # discovery under rules that did not exist then.
                detail=f"recheck after a rules change — {verdict.as_event()}",
            )
    counts.update({f"rule:{name}": n for name, n in sorted(by_rule.items())})
    return counts
