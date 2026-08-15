"""Select a resume from the library. The engine's core: pick, never compose.

The model returns `{variant, bullet_ids, trims?}` and nothing else. Title and
skills_order are **deterministic post-processing**, not model output — if the
model emits them they are dropped. Every emitted line is the library text
verbatim or a guarded trim of it: a trim may only *delete* whole clauses, may not
drop a `must_keep` phrase or a denylist token, and is checked against its source
bullet. On any failure — model error, invalid JSON, or validation that cannot be
salvaged — the chosen variant's **default selection** is used, which is itself
pre-approved and valid (spec §5). That fallback is a first-class path, not an
exception handler.

Rendering is a separate step (Checkpoint 3): this module produces a validated
`Selection`; it does not typeset.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import llm, prefilter, queries, tailor

# The variant-eligibility vocabulary. `framing` is closed to these; domain lives
# in `tags`. Kept here too so a bad framing is caught at select time, not only load.
FRAMING_VOCAB = frozenset({"general", "fullstack", "backend", "any"})

# Interior tokens a trim may never drop. Dropping a negation or a hedge inflates
# the claim while passing a naive subset check. Mirrors docs/resume-redesign.md §4.
_DENYLIST_WORDS = frozenset({
    "not", "never", "no", "only", "pilot", "delivered",
})
_DENYLIST_PHRASES = ("up to", "evaluation corpus")
# "of <N>" — the yield form ("16 of 80"); dropping "of 80" turns a fraction into a total.
_OF_N = re.compile(r"\bof\s+\d")

# Clause boundaries a trim may cut at. A deletion that is not a whole clause
# segment — an interior function word, half a clause — is rejected.
_CLAUSE_DELIM = re.compile(r"\s*[—;()]\s*|,\s+")

# Content tokens for the subsequence check: words, numbers, percents, glued
# identifiers. Pure punctuation is dropped; case is normalized.
_TOK = re.compile(r"[A-Za-z0-9][A-Za-z0-9%.+#/-]*")


class SelectError(RuntimeError):
    """The selection could not be produced or validated."""


@dataclass
class SelectedBullet:
    id: str
    entry_key: str
    text: str          # final: library text verbatim, or the validated trim
    source_text: str   # the library text
    trimmed: bool = False
    findings: list[str] = field(default_factory=list)


@dataclass
class Selection:
    variant: str
    title: str
    summary: str
    skills: list[tuple[str, list[str]]]   # ordered groups -> ordered skills
    bullets: list[SelectedBullet]
    fell_back: bool = False
    findings: list[str] = field(default_factory=list)


# =============================== deterministic ===============================


def title_for(conn: sqlite3.Connection, jd_title: str | None) -> str:
    """The resume title line — deterministic, never the model's.

    The JD's title, unless it states a seniority the candidate cannot support, in
    which case the variant's default (`resume_meta.title_default`) is used. Reuses
    the scoring prefilter's tested level detector so the two never disagree about
    what "senior" means.
    """
    default = str(queries.resume_meta(conn, "title_default") or "Software Engineer")
    text = (jd_title or "").strip()
    if not text:
        return default
    if prefilter.detect_level(text) in ("senior", "above"):
        return default
    return text


def _jd_relevance(item: str, jd_lower: str) -> int:
    """How strongly the JD asks for a skill. 0 = not mentioned."""
    lowered = item.lower().strip()
    if not lowered or not jd_lower:
        return 0
    if lowered in jd_lower:
        return 3
    head = re.split(r"[\s(/,+]", lowered, 1)[0].strip()
    if len(head) > 2 and head in jd_lower:
        return 2
    words = [w for w in re.split(r"[^a-z0-9.+#]+", lowered) if len(w) > 3]
    return 1 if any(w in jd_lower for w in words) else 0


# Group dropped unless a peptide/ML bullet is on the resume — an unbacked skill
# group otherwise. Spec §6 step 5.
_ML_GROUP = "ML / Scientific"


def skills_order(
    conn: sqlite3.Connection, jd_text: str, *, keep_ml: bool
) -> list[tuple[str, list[str]]]:
    """Skills grouped and reordered for the JD — deterministic, never the model's.

    Ranks `skills_master ∩ JD` to the front, stable-sorts items within each group,
    reorders groups by their best member, and drops the ML group unless a peptide
    bullet is selected. Asserts the result is a permutation/subset of the master —
    no skill the candidate cannot back is ever introduced.
    """
    master = queries.resume_skill_groups(conn)
    jd_lower = (jd_text or "").lower()
    ranked: list[tuple[str, list[str]]] = []
    for group, items in master:
        if group == _ML_GROUP and not keep_ml:
            continue
        ordered = sorted(items, key=lambda s: -_jd_relevance(s, jd_lower))
        ranked.append((group, ordered))
    ranked.sort(key=lambda gi: -max((_jd_relevance(s, jd_lower) for s in gi[1]), default=0))

    # Permutation/subset assertion: every emitted item exists in the master group.
    master_map = {g: set(items) for g, items in master}
    for group, items in ranked:
        if group not in master_map or not set(items) <= master_map[group]:
            raise SelectError(
                f"skills_order for group {group!r} is not a subset of skills_master — "
                f"a skill was introduced that the candidate cannot back"
            )
    return ranked


# ================================ trim guard ================================


def _toks(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOK.finditer(text)]


def _subsequence_kept(source: list[str], trim: list[str]) -> list[bool] | None:
    """Greedy left-alignment. Returns a kept-mask over `source` if `trim` is a
    subsequence of it (only deletions, order preserved), else None."""
    kept = [False] * len(source)
    j = 0
    for i, tok in enumerate(source):
        if j < len(trim) and trim[j] == tok:
            kept[i] = True
            j += 1
    return kept if j == len(trim) else None


def _segment_index(source_text: str) -> list[int]:
    """Segment id per source content token, split on clause boundaries."""
    seg_of: list[int] = []
    for seg_id, segment in enumerate(_CLAUSE_DELIM.split(source_text)):
        for _ in _toks(segment):
            seg_of.append(seg_id)
    return seg_of


def validate_trim(bullet: dict[str, Any], trim: str) -> list[str]:
    """Every reason `trim` is not an allowed shortening of `bullet['text']`.

    Empty list = the trim is safe to use. A non-empty list means fall back to the
    verbatim library text — a trim is a convenience, never a requirement.
    """
    source = str(bullet["text"])
    problems: list[str] = []
    src_toks, trim_toks = _toks(source), _toks(trim)

    kept = _subsequence_kept(src_toks, trim_toks)
    if kept is None:
        # Not a pure deletion — the model reworded or added. Reject outright; this
        # is what subsumes no_add / no_upgrade and any new number, id, or letter.
        return ["not a subsequence of the source — a trim may only delete, not reword"]

    # Clause-boundary: each source segment is fully kept or fully dropped.
    seg_of = _segment_index(source)
    if len(seg_of) == len(src_toks):
        from collections import defaultdict
        by_seg: dict[int, list[bool]] = defaultdict(list)
        for flag, seg in zip(kept, seg_of):
            by_seg[seg].append(flag)
        if any(any(flags) and not all(flags) for flags in by_seg.values()):
            problems.append("deletes part of a clause — trims must cut at clause boundaries")

    # must_keep phrases survive (verbatim substring, case-insensitive).
    low = trim.lower()
    for phrase in bullet.get("must_keep") or []:
        if str(phrase).lower() not in low:
            problems.append(f"drops must_keep phrase {phrase!r}")

    # Denylist tokens present in the source may not be dropped.
    src_low = source.lower()
    for word in _DENYLIST_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", src_low) and not re.search(rf"\b{re.escape(word)}\b", low):
            problems.append(f"drops denylisted token {word!r}")
    for phrase in _DENYLIST_PHRASES:
        if phrase in src_low and phrase not in low:
            problems.append(f"drops denylisted phrase {phrase!r}")
    if _OF_N.search(src_low) and not _OF_N.search(low):
        problems.append("drops an 'of <N>' yield — turns a fraction into a total")

    return problems


# ============================== the validator ==============================


def validate_selection(
    conn: sqlite3.Connection,
    variant: str,
    bullet_ids: list[str],
    trims: dict[str, str] | None,
) -> list[SelectedBullet]:
    """The repurposed 9-rule gate over a library selection.

    ids exist · no duplicate · tier != interview · claim_group exclusivity (<=1
    per group) · per-trim guard. A trim that cannot pass falls back to the
    verbatim library text — keep-whole-or-drop, never mangled. Raises only on a
    structural impossibility (an id the library does not have, an empty selection);
    honesty objections downgrade a trim rather than failing the resume.
    """
    trims = trims or {}
    library = {b["id"]: b for b in queries.resume_bullets(conn)}
    if not bullet_ids:
        raise SelectError("the selection is empty")

    seen: set[str] = set()
    by_group: dict[str, str] = {}
    out: list[SelectedBullet] = []
    for bid in bullet_ids:
        if bid not in library:
            raise SelectError(f"selected bullet {bid!r} is not in the library")
        if bid in seen:
            raise SelectError(f"bullet {bid!r} selected more than once")
        seen.add(bid)
        b = library[bid]
        if b["tier"] == "interview":
            raise SelectError(f"bullet {bid!r} is interview-tier and never goes on a resume")
        group = b["claim_group"]
        if group in by_group:
            raise SelectError(
                f"bullets {by_group[group]!r} and {bid!r} share claim_group {group!r} — "
                f"a resume shows at most one per group"
            )
        by_group[group] = bid

        source_text = str(b["text"])
        text, trimmed, findings = source_text, False, []
        proposed = (trims.get(bid) or "").strip()
        if proposed and proposed != source_text:
            objections = validate_trim(b, proposed)
            if objections:
                # Keep-whole-or-drop: the trim is rejected, the verbatim line stands.
                findings = [f"trim rejected, kept verbatim: {'; '.join(objections)}"]
            else:
                text, trimmed = proposed, True
        out.append(SelectedBullet(
            id=bid, entry_key=b["entry_key"], text=text,
            source_text=source_text, trimmed=trimmed, findings=findings,
        ))
    return out


# ============================== model plumbing ==============================


def library_numbers(conn: sqlite3.Connection) -> set[str]:
    """Every number the library states, for the §7 checks the prose surfaces run.

    The library replaces the old corpus as the fact source: a scoring prefix, a
    gap sentence, or a form answer may quote any figure the resume can back and
    no other. Same shape as `tailor.corpus_numbers`, over `resume_bullets`.
    """
    hay = " ".join(str(b["text"]) for b in queries.resume_bullets(conn))
    return set(tailor._numbers(hay)) | tailor._spelled_numbers(hay)


def library_block(conn: sqlite3.Connection) -> str:
    """The cacheable library the selector reads — byte-identical across calls."""
    lines = ["RESUME LIBRARY", ""]
    for group, items in queries.resume_skill_groups(conn):
        lines.append(f"skills[{group}]: {', '.join(items)}")
    lines.append("")
    for v in queries.resume_variants(conn):
        lines.append(f"VARIANT {v['name']}: summary={v['summary_key']}")
    lines.append("")
    lines.append("BULLETS (id · entry · tier · framing · tags)")
    for b in queries.resume_bullets(conn):
        fr = b["framing"] if isinstance(b["framing"], str) else "/".join(b["framing"] or [])
        lines.append(
            f"[{b['id']}] {b['entry_key']} · {b['tier']} · {fr} · "
            f"{','.join(b['tags'])}\n    {b['text']}"
        )
    return "\n".join(lines)


def select_prompt(jd_text: str, limit_note: str = "") -> str:
    return (
        "JOB DESCRIPTION\n\n"
        f"{jd_text.strip()}\n\n"
        "Choose the variant and the bullets that best evidence fitness for this "
        "posting. Honor claim_group exclusivity (at most one bullet per group), "
        "never select an interview-tier bullet, and prefer an is_lead_candidate "
        "bullet to open each role. Return ONLY the JSON object described in your "
        "instructions: {\"variant\", \"bullet_ids\", \"trims\"}. Do not return a "
        "title or a skills list — those are set deterministically."
        + (f"\n\n{limit_note}" if limit_note else "")
    )


def parse_selection(raw: str) -> tuple[str, list[str], dict[str, str]]:
    """Pull `{variant, bullet_ids, trims?}` from the reply. Raises on anything else."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise SelectError(f"no JSON object in the reply: {raw[:200]!r}")
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SelectError(f"reply is not valid JSON: {exc}") from exc
    variant = str(payload.get("variant") or "").strip()
    ids = [str(x).strip() for x in (payload.get("bullet_ids") or []) if str(x).strip()]
    trims_raw = payload.get("trims") or {}
    trims = {str(k): str(v) for k, v in trims_raw.items()} if isinstance(trims_raw, dict) else {}
    if not variant or not ids:
        raise SelectError("reply is missing `variant` or `bullet_ids`")
    return variant, ids, trims


# ================================ fallback ================================


def default_selection(conn: sqlite3.Connection, variant: str) -> list[str]:
    """The variant's pre-approved default bullet ids, in render order."""
    ids: list[str] = []
    for entry in queries.resume_variant_entries(conn, variant):
        ids.extend(entry["default_bullets"])
    return ids


def _assemble(
    conn: sqlite3.Connection,
    variant: str,
    bullets: list[SelectedBullet],
    jd_text: str,
    jd_title: str | None,
    *,
    fell_back: bool,
    findings: list[str],
) -> Selection:
    variants = {v["name"]: v for v in queries.resume_variants(conn)}
    if variant not in variants:
        raise SelectError(f"unknown variant {variant!r}")
    summaries = queries.resume_summaries(conn)
    v = variants[variant]
    keep_ml = any(b.entry_key == "PEP" for b in bullets)
    return Selection(
        variant=variant,
        title=title_for(conn, jd_title),
        summary=summaries.get(v["summary_key"], ""),
        skills=skills_order(conn, jd_text, keep_ml=keep_ml),
        bullets=bullets,
        fell_back=fell_back,
        findings=findings,
    )


def fallback(
    conn: sqlite3.Connection,
    variant: str,
    jd_text: str,
    reason: str,
    *,
    jd_title: str | None = None,
) -> Selection:
    """The variant default, rendered verbatim. First-class, not an error path."""
    if variant not in {v["name"] for v in queries.resume_variants(conn)}:
        variant = "general"
    bullets = validate_selection(conn, variant, default_selection(conn, variant), None)
    return _assemble(
        conn, variant, bullets, jd_text, jd_title, fell_back=True,
        findings=[f"used the {variant} default selection: {reason}"],
    )


def select(
    conn: sqlite3.Connection,
    jd_text: str,
    *,
    jd_title: str | None = None,
    application_id: int | None = None,
    expect_repeat: bool = True,
) -> Selection:
    """One selection pass: model -> parse -> validate, with the default as the floor.

    The model picks `{variant, bullet_ids, trims}`. Everything the model cannot be
    trusted with — the title, the skills, whether a trim is honest — is decided
    here. Any failure resolves to the variant default rather than blocking.
    """
    if not jd_text.strip():
        raise SelectError("no job description to select against")
    corpus = library_block(conn)

    variant = "general"  # the floor, in case parsing fails before a variant is known
    try:
        raw = llm.complete(
            "select", select_prompt(jd_text), conn=conn, cached=corpus,
            application_id=application_id, expect_repeat=expect_repeat,
        )
        variant, ids, trims = parse_selection(raw)
        bullets = validate_selection(conn, variant, ids, trims)
    except (llm.LLMError, SelectError) as exc:
        return fallback(conn, variant, jd_text, str(exc), jd_title=jd_title)

    return _assemble(conn, variant, bullets, jd_text, jd_title, fell_back=False, findings=[])
