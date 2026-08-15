"""Adversarial fixtures for the select-from-a-library engine (Phase 1 redesign).

Standalone, like the other suites — run by `make test`, not pytest. Builds a
throwaway database (migrate from empty + load the real library), which also makes
the stories case a genuine fresh-clone test: the exact bar Checkpoint 1's
clone-breakage flag required.

Covers every gate the redesign added: the selection gate, the trim guard
(subsequence / clause-boundary / must_keep / denylist), the deterministic title
and skills, the deterministic fallback path, strict link-gating, the swap-in
honesty carry, and fresh-clone story resolution.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

# A throwaway DB, chosen before any jobhunt import so config binds to it.
_TMP = tempfile.mkdtemp(prefix="jobhunt-select-test-")
os.environ["JOBHUNT_DB"] = os.path.join(_TMP, "test.db")
os.environ["JOBHUNT_DB_CREATE"] = "1"
os.environ.setdefault("JOBHUNT_SQLITE_SYNCHRONOUS", "OFF")

from jobhunt import llm, load_profile, migrate, queries, resume  # noqa: E402
from jobhunt import select as S  # noqa: E402
from jobhunt.db import connect  # noqa: E402


def _setup():
    with contextlib.redirect_stdout(io.StringIO()):
        migrate.main()
        conn = connect()
        load_profile.load(conn)
    return conn


CONN = _setup()
RESULTS: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    RESULTS.append((bool(cond), label))


# ------------------------------------------------------------------ loader ---

check(len(queries.resume_bullets(CONN)) == 42, "loader: 42 library bullets")
check(len(queries.resume_variants(CONN)) == 3, "loader: 3 variants")
check(len(queries.resume_swap_entries(CONN)) == 2, "loader: 2 swap entries (PEP/FPS)")


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except S.SelectError:
        return True


# -------------------------------------------------------- selection gate ---

check(_raises(lambda: S.validate_selection(CONN, "backend", ["SIR-pickle"], None)),
      "gate: interview-tier bullet rejected")
check(_raises(lambda: S.validate_selection(CONN, "backend", ["ARC-mem-full", "ARC-mem-short"], None)),
      "gate: two from one claim_group rejected")
check(_raises(lambda: S.validate_selection(CONN, "backend", ["NOPE-1"], None)),
      "gate: unknown bullet id rejected")
check(_raises(lambda: S.validate_selection(CONN, "backend", [], None)),
      "gate: empty selection rejected")
check(len(S.validate_selection(CONN, "backend", S.default_selection(CONN, "backend"), None)) == 10,
      "gate: the backend default validates")


# ------------------------------------------------------------- trim guard ---

_ARC = next(b for b in queries.resume_bullets(CONN) if b["id"] == "ARC-mem-full")
_paren = ("Diagnosed a memory leak that grew a long-running service to 30 GB, traced "
          "~90% to ML models being reloaded on every upload, and fixed six sources; "
          "the server then ran a full month of continuous use without recurrence.")
check(S.validate_trim(_ARC, _paren) == [], "trim: clause-boundary deletion accepted")
check(S.validate_trim(_ARC, "Diagnosed a memory leak that grew a long-running service to "
                            "30 GB and fixed six sources."),
      "trim: dropping must_keep 'a full month' rejected")
check(S.validate_trim(_ARC, "Fixed a memory leak: six sources, 30 GB, one month clean."),
      "trim: rewording (not a subsequence) rejected")
_RLS = next(b for b in queries.resume_bullets(CONN) if b["id"] == "FBT-rls")
check(S.validate_trim(_RLS, _RLS["text"].replace("session-based ", "")),
      "trim: interior deletion (mid-clause) rejected")
_SCOPE = next(b for b in queries.resume_bullets(CONN) if b["id"] == "FBT-scope")
check(S.validate_trim(_SCOPE, _SCOPE["text"].replace(" never", "")),
      "trim: dropping denylist 'never' rejected")


# ------------------------------------------------------- deterministic title ---

check(S.title_for(CONN, "Senior Backend Engineer") == "Software Engineer",
      "title: 'Senior' falls back to the default")
check(S.title_for(CONN, "Staff Software Engineer") == "Software Engineer",
      "title: 'Staff' falls back to the default")
check(S.title_for(CONN, "Backend Engineer") == "Backend Engineer",
      "title: an early-career title is kept")
check(S.title_for(CONN, "") == "Software Engineer",
      "title: empty falls back to resume_meta.title_default")


# ------------------------------------------------------- deterministic skills ---

_so = S.skills_order(CONN, "We use React, TypeScript and Postgres.", keep_ml=False)
_master = {g: set(items) for g, items in queries.resume_skill_groups(CONN)}
check(all(set(items) <= _master.get(g, set()) for g, items in _so),
      "skills: output is a subset of skills_master (no new item)")
check(not any(g == "ML / Scientific" for g, _ in _so),
      "skills: ML group dropped when no peptide bullet")
check(any(g == "ML / Scientific" for g, _ in S.skills_order(CONN, "AlphaFold", keep_ml=True)),
      "skills: ML group kept when a peptide bullet is present")


# --------------------------------------------------------- fallback path ---

_orig = llm.complete
llm.complete = lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("boom"))
try:
    _fb = S.select(CONN, "Backend role.", jd_title="Senior Staff Engineer")
finally:
    llm.complete = _orig
check(_fb.fell_back and _fb.variant == "general" and len(_fb.bullets) > 0,
      "fallback: model error -> variant default, first-class")
check(_fb.title == "Software Engineer",
      "fallback: title still guarded on the fallback path")


# ------------------------------------------------- link-gating + swap-in ---

def _build(variant, ids, jd, title):
    bullets = S.validate_selection(CONN, variant, ids, None)
    return S._assemble(CONN, variant, bullets, jd, title, fell_back=False, findings=[])


_research = _build("backend", ["FBT-rls", "FBT-llm-b", "SIR-docker", "ARC-mem-full", "PEP-combined"],
                   "ML research: protein design.", "Machine Learning Engineer")
_doc_r = resume.build_selection_document(CONN, _research)
_proj_r = _doc_r["cv"]["sections"].get("projects", [])
check(any(p["name"] == "peptideDesign" for p in _proj_r),
      "swap-in: peptideDesign rendered for a research JD")
_pep = next(p for p in _proj_r if p["name"] == "peptideDesign")
check("unpaid" in _pep.get("summary", "").lower() and not _pep["name"].startswith("["),
      "swap-in: PEP descriptor verbatim (honest) and no link")

_frontend = _build("fullstack", ["FBT-ship-g", "SIR-csv-fs", "ARC-arch-short", "FPS-mp"],
                   "Frontend/game: React, real-time.", "Frontend Engineer")
_doc_f = resume.build_selection_document(CONN, _frontend)
_fps = next(p for p in _doc_f["cv"]["sections"]["projects"] if p["name"] == "FireProofSheep")
_rp = (queries.resume_meta(CONN, "open_facts") or {}).get("repos_public", {})
check(_rp.get("FireProofSheep") == "demo_live_leaderboard_broken" and not _fps["name"].startswith("["),
      "link-gating: FireProofSheep truthy-string value prints no link (strict == true)")

# Every rendered highlight, both docs, is a verbatim library string.
_lib = {b["text"] for b in queries.resume_bullets(CONN)}
_hl = [h for doc in (_doc_r, _doc_f) for s in doc["cv"]["sections"].values()
       for e in s if isinstance(e, dict) for h in e.get("highlights", [])]
check(all(h in _lib for h in _hl), "renderer: every highlight verbatim from the library")


# ------------------------------------------ stories: fresh-clone resolution ---

_total = CONN.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
_attached = CONN.execute("SELECT COUNT(*) FROM stories WHERE entry_key IS NOT NULL").fetchone()[0]
check(_total > 0 and _attached == _total,
      f"stories: fresh clone resolves all {_total} by entry_key, none unattached")


# ---------------------------------------------------------------- report ---

_passed = sum(1 for ok, _ in RESULTS if ok)
for ok, label in RESULTS:
    print(f"  {'ok    ' if ok else 'FAIL  '}{label}")
print(f"\n{_passed}/{len(RESULTS)} cases behaved correctly")
sys.exit(0 if _passed == len(RESULTS) else 1)
