"""Discovery: JobSpy -> normalize -> dedup -> `discovered`.

One search sweep per run, driven entirely by `config/searches.yaml`. Everything
that comes back is written as a `discovered` application. Nothing is filtered
here — `score` runs the deterministic prefilter from `docs/profile/scoring.yaml`
and this worker must not second-guess it, or a posting gets dropped in a place
with no record that it ever existed.

Idempotent by construction. `jobs.apply_url_norm` is UNIQUE, so a posting seen on
the second run of the day inserts nothing and the row count says so. There is no
"last run" cursor to get out of step: rerunning is always safe.

Output is counts only. Discovered postings are read on the dashboard, not out of
a log — and a log line naming an employer is exactly what CLAUDE.local.md rules
out, whether or not it happens to be my current one.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import config, queries, states
from .db import connect
from .normalize import UnparseableURL, detect_ats, normalize_apply_url

SEARCHES_YAML = config.REPO_ROOT / "config" / "searches.yaml"

# `interval` as JobSpy reports it -> multiplier to a yearly figure. Indeed states
# hourly rates for contract work often enough that dropping them would lose real
# numbers, and comp exists to answer with. 2080 = 40h x 52w.
_ANNUALIZE: dict[str, float] = {
    "yearly": 1.0,
    "annual": 1.0,
    "monthly": 12.0,
    "weekly": 52.0,
    "daily": 260.0,
    "hourly": 2080.0,
}


class IngestError(RuntimeError):
    """The search config is unusable, or JobSpy is not installed."""


@dataclass
class Sweep:
    """What one run did. Counts only — see the module docstring."""

    found: int = 0
    inserted: int = 0
    duplicates: int = 0
    unparseable: int = 0
    incomplete: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    empty_searches: list[str] = field(default_factory=list)
    searches: int = 0

    def line(self) -> str:
        parts = [
            f"{self.searches} searches",
            f"{self.found} found",
            f"{self.inserted} new",
            f"{self.duplicates} already tracked",
        ]
        if self.incomplete:
            parts.append(f"{self.incomplete} skipped (missing title/company/url)")
        if self.unparseable:
            parts.append(f"{self.unparseable} skipped (unparseable url)")
        return " · ".join(parts)


def load_config() -> dict[str, Any]:
    if not SEARCHES_YAML.exists():
        raise IngestError(f"{SEARCHES_YAML} is missing.")
    try:
        loaded = yaml.safe_load(SEARCHES_YAML.read_text()) or {}
    except yaml.YAMLError as exc:
        raise IngestError(f"{SEARCHES_YAML.name} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise IngestError(f"{SEARCHES_YAML.name}: expected a mapping at the top level")
    for key in ("sites", "terms", "locations"):
        if not loaded.get(key):
            raise IngestError(f"{SEARCHES_YAML.name}: `{key}` is required and is empty")
    return loaded


# ================================= normalizing =================================


def _text(value: Any) -> str | None:
    """A pandas cell as a string, or None. NaN is a float, and `str(nan)` is
    `'nan'` — which is how a missing description becomes the literal text
    "nan" in a column three phases from here."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _money(value: Any, interval: str | None) -> int | None:
    """A salary figure annualized to whole dollars, or None.

    JobSpy hands these back as floats-in-strings (`'235000.0'`). An interval it
    does not recognise returns None rather than a guess: a wrong number here
    gets quoted on a real application form.
    """
    raw = _text(value)
    if raw is None:
        return None
    try:
        amount = float(raw.replace(",", "").replace("$", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    multiplier = _ANNUALIZE.get((interval or "yearly").strip().lower())
    if multiplier is None:
        return None
    return int(round(amount * multiplier))


def _remote(row: dict[str, Any]) -> str:
    """`jobs.remote` is onsite | hybrid | remote | unknown.

    JobSpy's `is_remote` is a bool-ish; `work_from_home_type` carries Indeed's
    own wording when it has it, which is the only place hybrid shows up.
    """
    wfh = (_text(row.get("work_from_home_type")) or "").lower()
    if "hybrid" in wfh:
        return "hybrid"
    if "remote" in wfh:
        return "remote"
    flag = _text(row.get("is_remote"))
    if flag and flag.lower() in {"true", "1", "yes"}:
        return "remote"
    if flag and flag.lower() in {"false", "0", "no"}:
        return "onsite"
    return "unknown"


def _apply_url(row: dict[str, Any]) -> str | None:
    """The URL to dedup and apply on.

    `job_url_direct` is the employer's own ATS link and `job_url` is the Indeed
    interstitial, so the direct one is strongly preferred: two aggregators
    listing one Greenhouse posting produce one key from the direct URL and two
    from their own. It also feeds `detect_ats`, which is how the stats page
    knows a Workday from a Lever.
    """
    return _text(row.get("job_url_direct")) or _text(row.get("job_url"))


def _posted_at(value: Any) -> str | None:
    raw = _text(value)
    if raw is None:
        return None
    try:
        return config.to_utc_timestamp(raw)
    except ValueError:
        # A malformed date is not worth losing the posting over — `posted_at` is
        # nullable, and the staleness check reads NULL as "unknown" rather than
        # being poisoned by a garbage value.
        return None


# =================================== running ===================================


def search(cfg: dict[str, Any], term: str, location: dict[str, Any]) -> list[dict[str, Any]]:
    """One JobSpy call. Returns plain dicts so nothing downstream sees pandas."""
    try:
        import jobspy
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise IngestError(
            "the python-jobspy package is not installed. "
            "Install it with: uv pip install -e '.[ingest]'"
        ) from exc

    is_remote = bool(location.get("remote"))
    city = _text(location.get("city"))
    kwargs: dict[str, Any] = {
        "site_name": list(cfg["sites"]),
        "search_term": term,
        # Only read by the google scraper, which currently returns nothing. Kept
        # so re-enabling google is a one-line change in searches.yaml.
        "google_search_term": f"{term} jobs" + (f" near {city}" if city else ""),
        "results_wanted": int(cfg.get("results_wanted", 40)),
        "hours_old": int(cfg["hours_old"]) if cfg.get("hours_old") else None,
        "country_indeed": cfg.get("country_indeed", "usa"),
        "description_format": "markdown",
        "is_remote": is_remote,
        "verbose": 0,
    }
    if city:
        kwargs["location"] = city

    try:
        frame = jobspy.scrape_jobs(**kwargs)
    except Exception as exc:
        # One dead source must not lose the sweep. Indeed rate-limits, and a run
        # that gets three metros in before being throttled should keep those.
        print(f"  ! {term} / {city or 'remote'}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []
    if frame is None or not len(frame):
        return []
    return frame.to_dict("records")


def store(conn: sqlite3.Connection, rows: list[dict[str, Any]], sweep: Sweep) -> None:
    """Write one search's results. Each posting is its own transaction.

    Deliberately not one transaction for the sweep: a sweep is a few hundred
    postings over several minutes, and a failure on the last one should not
    discard the first three hundred. The dedup guard is a UNIQUE index, not
    anything held in memory, so a partial run resumes correctly.
    """
    for row in rows:
        sweep.found += 1
        title = _text(row.get("title"))
        company = _text(row.get("company"))
        apply_url = _apply_url(row)
        if not (title and company and apply_url):
            sweep.incomplete += 1
            continue

        try:
            normalize_apply_url(apply_url)
        except UnparseableURL:
            sweep.unparseable += 1
            continue

        site = _text(row.get("site")) or "unknown"
        source = f"jobspy:{site}"
        ats_type, ats_slug = detect_ats(apply_url)
        interval = _text(row.get("interval"))

        try:
            with conn:  # one posting, committed or not at all
                company_id = queries.upsert_company(
                    conn, company, ats_type=ats_type, ats_slug=ats_slug
                )
                job_id = queries.insert_job(
                    conn,
                    company_id=company_id,
                    title=title,
                    apply_url=apply_url,
                    source=source,
                    location=_text(row.get("location")),
                    remote=_remote(row),
                    jd_text=_text(row.get("description")),
                    comp_min=_money(row.get("min_amount"), interval),
                    comp_max=_money(row.get("max_amount"), interval),
                    posted_at=_posted_at(row.get("date_posted")),
                )
                states.create(
                    conn, job_id=job_id, state=states.DISCOVERED, detail=source
                )
        except sqlite3.IntegrityError:
            # `apply_url_norm` is UNIQUE. This is the guard working, not an
            # error: the same posting comes back on every run inside the window.
            sweep.duplicates += 1
            continue
        sweep.inserted += 1
        sweep.per_source[source] = sweep.per_source.get(source, 0) + 1


def run(conn: sqlite3.Connection, cfg: dict[str, Any] | None = None) -> Sweep:
    """One full sweep: every term against every location."""
    cfg = cfg or load_config()
    sweep = Sweep()
    delay = float(cfg.get("delay_seconds", 3))
    pairs = [(term, loc) for term in cfg["terms"] for loc in cfg["locations"]]

    for index, (term, location) in enumerate(pairs):
        rows = search(cfg, term, location)
        sweep.searches += 1
        where = _text(location.get("city")) or "remote"
        if not rows:
            sweep.empty_searches.append(f"{term} / {where}")
        store(conn, rows, sweep)
        if delay and index < len(pairs) - 1:
            time.sleep(delay)
    return sweep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One discovery sweep.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="search and report counts without writing anything",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="stop after N searches (for checking a change)"
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except IngestError as exc:
        print(f"ingest not run: {exc}", file=sys.stderr)
        return 1
    if args.limit:
        cfg = {**cfg, "terms": cfg["terms"][: args.limit], "locations": cfg["locations"][:1]}

    conn = connect()
    try:
        if args.dry_run:
            sweep = Sweep()
            for term in cfg["terms"]:
                for location in cfg["locations"]:
                    rows = search(cfg, term, location)
                    sweep.searches += 1
                    sweep.found += len(rows)
                    if not rows:
                        sweep.empty_searches.append(
                            f"{term} / {_text(location.get('city')) or 'remote'}"
                        )
            print(f"dry run — {sweep.searches} searches, {sweep.found} postings, nothing written")
        else:
            sweep = run(conn, cfg)
            print(sweep.line())
            for source, count in sorted(sweep.per_source.items()):
                print(f"  {source}: {count}")
    except IngestError as exc:
        print(f"ingest not run: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    # A source that has quietly died returns zero rows rather than raising, which
    # is how discovery stops without anyone noticing. Say so every run.
    if sweep.empty_searches:
        print(
            f"\n{len(sweep.empty_searches)} of {sweep.searches} searches returned nothing:",
            file=sys.stderr,
        )
        for label in sweep.empty_searches[:10]:
            print(f"  - {label}", file=sys.stderr)
        if len(sweep.empty_searches) > 10:
            print(f"  ... and {len(sweep.empty_searches) - 10} more", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
