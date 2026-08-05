"""Discovery: Indeed scrape and direct ATS board polls -> dedup -> `discovered`.

Two sources with opposite risk profiles, both driven by `config/searches.yaml`:

  * JobSpy against Indeed. Works, and is one Cloudflare rule from not working.
    The only source that can get us banned, so it is paced, jittered, and gives
    up early when it starts being refused.
  * `boards.py` against Greenhouse, Lever, and Ashby's public JSON. Documented
    endpoints meant for machine reads, throttled per host.

Scraped rows are written as they come: Indeed applies the search term
server-side, so that source never returns a warehouse inspector. Board rows are
filtered on title first — a board poll has no query and returns the whole
company. See `poll_boards` for why that is not the `score` prefilter wearing a
disguise. Everything that lands still goes through `score` in full.

Idempotent by construction. `jobs.apply_url_norm` is UNIQUE, so a posting seen on
the second run of the day inserts nothing and the row count says so. There is no
"last run" cursor to get out of step: rerunning is always safe.

Output is counts only. Discovered postings are read on the dashboard, not out of
a log — and a log line naming an employer is exactly what CLAUDE.local.md rules
out, whether or not it happens to be my current one.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import boards, config, queries, runs, states
from .db import connect, transaction
from .normalize import UnparseableURL, detect_ats, normalize_apply_url
from .runs import ProgressFn

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
    aborted_scrape: str | None = None
    boards_polled: int = 0
    board_failures: list[str] = field(default_factory=list)
    filtered_by_title: int = 0

    def line(self) -> str:
        parts = [
            f"{self.searches} searches",
            f"{self.boards_polled} boards",
            f"{self.found} found",
            f"{self.inserted} new",
            f"{self.duplicates} already tracked",
        ]
        if self.filtered_by_title:
            # Named, every run. This is the only place a posting is dropped
            # without leaving a row, so the number has to be in front of me —
            # a filter quietly widening is how the digest goes quiet.
            parts.append(f"{self.filtered_by_title} boards rows failed the title rule")
        if self.incomplete:
            parts.append(f"{self.incomplete} skipped (missing title/company/url)")
        if self.unparseable:
            parts.append(f"{self.unparseable} skipped (unparseable url)")
        return " · ".join(parts)

    def tally(self) -> dict[str, int]:
        """The counters worth watching while it is still going.

        Rendered as-is by the dashboard, in this order, so adding a counter here
        needs no frontend change. The last two are omitted at zero: "0 filtered"
        on every run is the kind of noise that stops a number being read at all.
        """
        counts = {"found": self.found, "new": self.inserted, "duplicates": self.duplicates}
        if self.filtered_by_title:
            counts["wrong title"] = self.filtered_by_title
        if self.incomplete or self.unparseable:
            counts["unusable"] = self.incomplete + self.unparseable
        return counts


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


def title_filter() -> tuple[list[str], list[str]]:
    """`(keywords, blocked)` from docs/profile/scoring.yaml, lowercased.

    Read here rather than duplicated: scoring.yaml already defines what a title
    has to contain, and having two lists to keep in step is how they diverge.
    A missing or empty file disables the filter rather than dropping everything.
    """
    path = config.PROFILE_DIR / "scoring.yaml"
    if not path.exists():
        return [], []
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise IngestError(f"scoring.yaml is not valid YAML: {exc}") from exc
    keywords = [str(k).strip().lower() for k in (loaded.get("title_keywords") or [])]
    blocked = [str(b).strip().lower() for b in (loaded.get("titles_block") or [])]
    return [k for k in keywords if k], [b for b in blocked if b]


def passes_title(title: str, keywords: list[str], blocked: list[str]) -> bool:
    """scoring.yaml's rule: contains any keyword, and none of the blocked words."""
    if not keywords:
        return True
    lowered = title.lower()
    if any(word in lowered for word in blocked):
        return False
    return any(word in lowered for word in keywords)


def poll_boards(
    cfg: dict[str, Any], on_progress: ProgressFn | None = None
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Every seeded ATS board. Returns `(rows, failures, filtered_out)`.

    A dead board is expected, not exceptional — companies rename boards, get
    acquired, and move ATS. One 404 must not cost the other fifty, so failures
    are collected and reported rather than raised.

    Titles are filtered here, and only here. This looks like the prefilter that
    docs/build-plan.md assigns to `score`, and it is deliberately not the same
    thing. The Indeed path sends `search_term="software engineer"`, which Indeed
    applies server-side — so that source never returns a warehouse inspector in
    the first place. A board poll has no query; it returns the entire company.
    Polling SpaceX and Anduril unfiltered means thousands of manufacturing
    technicians and mechanical engineers, which are not shots being discarded
    on unreliable data — they are a different profession. Applying scoring.yaml's
    own title rule here restores the symmetry the two sources would otherwise
    lack, and `score` still runs the full prefilter over everything that lands.

    Pacing lives in `boards._get`, which throttles per host rather than per run.
    A single sleep here would pace the loop as a whole and still send nineteen
    Ashby boards back to back.
    """
    seeds = cfg.get("boards") or {}
    keywords, blocked = title_filter()
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    filtered_out = 0
    total = sum(len(slugs or []) for slugs in seeds.values())
    polled = 0

    for vendor, slugs in seeds.items():
        for slug in slugs or []:
            if on_progress:
                # Named, not just counted. Fifty boards at a per-host throttle is
                # the slowest silent stretch of a sweep, and "which one is it
                # stuck on" is the only question worth asking while it runs.
                on_progress(
                    phase="boards", message=f"{vendor} · {slug}",
                    done=polled, total=total,
                )
            polled += 1
            try:
                fetched = boards.fetch(vendor, str(slug))
            except boards.RateLimited as exc:
                # The vendor asked us to slow down and we already backed off and
                # retried. Continuing through their remaining boards is how a
                # throttle becomes a ban, and this source has to still work
                # tomorrow. Abandon this vendor for the run; the rest are
                # different hosts with their own budget.
                remaining = [s for s in slugs if s != slug]
                failures.append(
                    f"{vendor}/{slug}: {exc} — skipped {len(remaining)} more "
                    f"{vendor} board(s) this run rather than pushing harder"
                )
                break
            except boards.BoardError as exc:
                failures.append(f"{vendor}/{slug}: {exc}")
                continue
            for row in fetched:
                title = _text(row.get("title"))
                # No title is a malformed row, not a rejected one — let `store`
                # count it as incomplete rather than hiding it in this total.
                if title and not passes_title(title, keywords, blocked):
                    filtered_out += 1
                    continue
                rows.append(row)
    return rows, failures, filtered_out


def store(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    sweep: Sweep,
    on_progress: ProgressFn | None = None,
) -> None:
    """Write one search's results. Each posting is its own transaction.

    Deliberately not one transaction for the sweep: a sweep is a few hundred
    postings over several minutes, and a failure on the last one should not
    discard the first three hundred. The dedup guard is a UNIQUE index, not
    anything held in memory, so a partial run resumes correctly.
    """
    for index, row in enumerate(rows):
        # Between postings, never inside the transaction below. Only the board
        # poll asks for this: it arrives with thousands of rows at once, where a
        # bar that sits at zero for ten seconds looks like a hang.
        if on_progress and index % 25 == 0:
            on_progress(done=index, counts=sweep.tally())
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
        # `jobs.source` distinguishes how a posting was found, which is the
        # column the aggregator-staleness check reads. 001_init.sql names the
        # convention: 'jobspy:indeed' for a scrape, 'greenhouse:direct' for the
        # vendor's own API.
        source = f"{site}:direct" if site in boards.FETCHERS else f"jobspy:{site}"
        ats_type, ats_slug = detect_ats(apply_url)
        interval = _text(row.get("interval"))

        try:
            # `db.transaction`, not `with conn:`. The connection runs with
            # isolation_level=None, so `with conn:` opens no transaction at all —
            # its __exit__ commits an autocommit connection, which is a no-op,
            # and each statement below had already committed on its own. The
            # comment that used to be here claimed atomicity the code did not
            # have: a `states.create` that failed after `insert_job` succeeded
            # left an orphan job with no application, and `apply_url_norm` being
            # UNIQUE meant every later run counted it as a duplicate and moved
            # on. The posting was then invisible forever.
            #
            # Nesting is why this is enough: `upsert_company` and `states.create`
            # both open `transaction(conn)` themselves and join this one rather
            # than committing early, so all four writes land together or not at
            # all. It is also fewer fsyncs than before, which the mini notices —
            # it runs synchronous=FULL.
            with transaction(conn):
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


def run(
    conn: sqlite3.Connection,
    cfg: dict[str, Any] | None = None,
    *,
    scrape: bool = True,
    poll: bool = True,
    on_progress: ProgressFn | None = None,
) -> Sweep:
    """One full sweep: every term against every location, then every board.

    Boards run second and deliberately so. Both sources reach the same
    companies, and whichever inserts first owns the row — so a posting the
    aggregator already has keeps `jobspy:indeed` as its source. That makes
    `jobs.source` answer "would direct polling have found this on its own?",
    which is the question worth asking while Indeed is the single point of
    failure.
    """
    cfg = cfg or load_config()
    sweep = Sweep()
    # A no-op keeps every call site below unconditional. The CLI passes nothing
    # and behaves exactly as it did before this existed.
    report: ProgressFn = on_progress or (lambda **_: None)

    if scrape:
        delay = float(cfg.get("delay_seconds", 8))
        pairs = [(term, loc) for term in cfg["terms"] for loc in cfg["locations"]]
        consecutive_empty = 0
        for index, (term, location) in enumerate(pairs):
            where = _text(location.get("city")) or "remote"
            report(
                phase="searching",
                message=f"{term} · {where}",
                done=index,
                total=len(pairs),
                counts=sweep.tally(),
            )
            rows = search(cfg, term, location)
            sweep.searches += 1
            if rows:
                consecutive_empty = 0
            else:
                sweep.empty_searches.append(f"{term} / {where}")
                consecutive_empty += 1
            store(conn, rows, sweep)
            # Again after the write, so the tallies on screen are what is in the
            # database rather than what was about to be.
            report(done=index + 1, counts=sweep.tally())

            # Indeed is a scrape, not an API, and it is the one source that can
            # ban us. A run of empty results is what throttling looks like from
            # this side — the scraper returns nothing rather than raising — so
            # stop asking. Grinding through thirty more searches while being
            # refused is how a soft throttle becomes a hard block, and this has
            # to still work tomorrow.
            if consecutive_empty >= 4:
                sweep.aborted_scrape = (
                    f"stopped after {sweep.searches} of {len(pairs)} searches: "
                    f"{consecutive_empty} in a row came back empty, which is what "
                    f"being throttled looks like"
                )
                break
            if index < len(pairs) - 1:
                # Jittered: a fixed interval is a fingerprint, and two runs a day
                # at exactly 8.0s apart is a pattern worth not having.
                time.sleep(delay + random.uniform(0, delay * 0.5))

    if poll:
        rows, failures, filtered = poll_boards(cfg, on_progress=report)
        seeds = cfg.get("boards") or {}
        sweep.boards_polled = sum(len(slugs or []) for slugs in seeds.values())
        sweep.board_failures = failures
        sweep.filtered_by_title = filtered
        report(
            phase="storing",
            message=f"Storing {len(rows)} board postings",
            done=0,
            total=len(rows),
            counts=sweep.tally(),
        )
        store(conn, rows, sweep, on_progress=report)
    report(phase="finished", message=sweep.line(), done=0, total=None, counts=sweep.tally())
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
    # Two sources with very different failure modes, so each can be run alone:
    # the scrape is slow and breaks when a site changes, the boards are fast and
    # break when a company renames one.
    parser.add_argument("--boards-only", action="store_true", help="skip the Indeed scrape")
    parser.add_argument("--scrape-only", action="store_true", help="skip the board poll")
    args = parser.parse_args(argv)
    if args.boards_only and args.scrape_only:
        print("--boards-only and --scrape-only are mutually exclusive", file=sys.stderr)
        return 2

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
            # The lock is taken here rather than inside `run` so a dry run — which
            # writes nothing and is the thing you reach for while the 06:30 agent
            # is still going — is never refused.
            with runs.track(conn, "ingest") as report:
                sweep = run(
                    conn, cfg,
                    scrape=not args.boards_only,
                    poll=not args.scrape_only,
                    on_progress=report,
                )
            print(sweep.line())
            for source, count in sorted(sweep.per_source.items()):
                print(f"  {source}: {count}")
    except runs.AlreadyRunning as exc:
        # 3, not 1: deploy/discover.sh treats a non-zero ingest as a bad morning
        # worth a red agent, and "the dashboard is already doing it" is not that.
        print(f"ingest not run: {exc}", file=sys.stderr)
        return 3
    except IngestError as exc:
        print(f"ingest not run: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    # Loudest first: giving up mid-scrape means Indeed is refusing us, and that
    # is the difference between a slow week and a dead source.
    if sweep.aborted_scrape:
        print(f"\nSCRAPE ABORTED — {sweep.aborted_scrape}", file=sys.stderr)
        print(
            "  Leave it alone for a few hours before rerunning. If it persists, "
            "raise `delay_seconds` in config/searches.yaml, and lean on the board\n"
            "  poll (`make ingest ARGS=--boards-only`) meanwhile — those are the "
            "vendors' own APIs and are unaffected.",
            file=sys.stderr,
        )

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

    # A board 404s when a company renames it, migrates ATS, or is acquired. The
    # seed list is hand-maintained, so it goes stale silently unless this says so.
    if sweep.board_failures:
        print(
            f"\n{len(sweep.board_failures)} of {sweep.boards_polled} boards failed "
            f"— prune or fix these in config/searches.yaml:",
            file=sys.stderr,
        )
        for label in sweep.board_failures:
            print(f"  - {label}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
