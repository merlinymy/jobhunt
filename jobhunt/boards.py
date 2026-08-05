"""Poll ATS job boards directly, for a seeded list of companies.

The second discovery source, and the durable one. `ingest` scrapes Indeed
through JobSpy, which works today and is one Cloudflare rule from not working;
these are the ATS vendors' own public JSON endpoints, documented and meant to be
read by other software. No HTML parsing, no bot detection, no cursor to lose.

CLAUDE.md called this out under "Aggregator staleness" as optional. It stopped
being optional when Google went JS-only: discovery had exactly one live source.

Three vendors, chosen because they are what the companies I actually target use
(157 of 553 postings in one Indeed sweep):

    greenhouse  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
    lever       https://api.lever.co/v0/postings/{slug}?mode=json
    ashby       https://api.ashbyhq.com/posting-api/job-board/{slug}

Each returns the whole board in one request, so the unit of work is a company,
not a query. That is the real difference from a keyword search: a board poll
cannot miss a posting because the title used a word I did not think to search
for. The filtering happens later, in `score`, where it is reviewable.

Rows come out in JobSpy's shape on purpose. `ingest.store()` is the only writer,
so normalization, dedup, and the `discovered` transition stay in one place and
cannot drift between sources.
"""

from __future__ import annotations

import html
import json
import random
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

# Identifies the client honestly and says how to stop it. These are free public
# endpoints run by vendors who owe us nothing; a contactable UA is the least a
# polite client does, and it is what gets a warning instead of a block.
#
# Set JOBHUNT_CONTACT so a fork identifies itself rather than whoever wrote this.
USER_AGENT = (
    "jobhunt/0.1 (personal job search, ~1 run per 12h; "
    f"contact {os.environ.get('JOBHUNT_CONTACT') or 'https://github.com/merlinymy/jobhunt'})"
)
TIMEOUT = 25

# Minimum seconds between requests to the same host, per host.
#
# A single global sleep is the wrong shape: it paces the *run* rather than any
# one vendor, so 19 Ashby boards in a row still arrive back to back. Ashby's
# unauthenticated posting API has an unofficial ceiling around 100 requests per
# minute; Greenhouse documents none, which is not permission. These are set an
# order of magnitude under anything anyone has published, because the job runs
# twice a day unattended and finishing in 50 seconds instead of 25 buys nothing.
_MIN_INTERVAL: dict[str, float] = {
    "api.ashbyhq.com": 1.0,
    "boards-api.greenhouse.io": 1.0,
    "api.lever.co": 1.0,
}
_DEFAULT_INTERVAL = 1.0

# Retries are for "come back later", never for "you asked wrong". A 404 board is
# permanently gone and retrying it three times just triples the load.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_CAP = 30.0

_last_request: dict[str, float] = {}
_throttle_lock = threading.Lock()


class BoardError(RuntimeError):
    """A board endpoint answered with something unusable."""


class RateLimited(BoardError):
    """The host asked us to slow down and kept asking."""


def _throttle(host: str) -> None:
    """Block until this host's minimum interval has elapsed.

    Jittered, so two runs a day do not arrive as an identifiable metronome and
    a retry storm cannot synchronise across hosts.
    """
    interval = _MIN_INTERVAL.get(host, _DEFAULT_INTERVAL)
    with _throttle_lock:
        now = time.monotonic()
        earliest = _last_request.get(host, 0.0) + interval
        wait = earliest - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_request[host] = now + random.uniform(0, interval * 0.25)


def _get(url: str) -> Any:
    """One throttled GET, retrying only what is worth retrying."""
    host = urllib.parse.urlsplit(url).hostname or ""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _throttle(host)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS:
                # 404 is the ordinary answer for a board renamed or taken down,
                # and a seed list goes stale on its own. Say which, keep going.
                error = RateLimited if exc.code == 429 else BoardError
                raise error(f"HTTP {exc.code} from {url}") from exc
            # Honour Retry-After when the server sends one — guessing shorter
            # than what we were told is how a slowdown becomes a block.
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = float(retry_after) if retry_after else 0.0
            except ValueError:
                delay = 0.0
            delay = max(delay, min(_BACKOFF_CAP, 2.0**attempt)) + random.uniform(0, 1)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == _MAX_ATTEMPTS:
                raise BoardError(f"{type(exc).__name__} from {url}: {exc}") from exc
            time.sleep(min(_BACKOFF_CAP, 2.0**attempt) + random.uniform(0, 1))
    raise BoardError(f"exhausted {_MAX_ATTEMPTS} attempts for {url}")  # pragma: no cover


def _iso(value: Any) -> str | None:
    """Whatever the vendor calls a timestamp -> `YYYY-MM-DD`, or None.

    Three formats across three vendors: Greenhouse sends ISO with an offset,
    Lever sends epoch milliseconds, Ashby sends a bare ISO date. `ingest` runs
    the result through `config.to_utc_timestamp`, which takes any of them, but
    epoch millis have to be converted here — nothing downstream would recognise
    `1754160000000` as a date, and it would silently store as NULL.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        seconds = float(value) / 1000.0
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None
    return str(value).strip() or None


_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")


def _plain(markup: str | None) -> str | None:
    """HTML(-escaped) job description -> readable text.

    Greenhouse returns `content` as *escaped* HTML: the literal characters
    `&lt;p&gt;`, not `<p>`. Stored raw, the scorer reads a page of entity noise
    instead of the posting, which costs tokens and buys nothing — and `tailor`
    would eventually quote it. Unescape first, then strip tags; block-level tags
    become newlines so the structure survives.
    """
    if not markup:
        return None
    text = html.unescape(str(markup))
    # A second pass: some boards double-escape, so one unescape leaves `&lt;p&gt;`
    # as `<p>` and another leaves the entities the first pass created.
    if "&lt;" in text or "&amp;" in text:
        text = html.unescape(text)
    text = re.sub(r"(?i)<(?:br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)  # entities that were inside tags
    text = _BLANKS.sub("\n\n", text)
    return text.strip() or None


def _row(
    *,
    title: str | None,
    company: str,
    apply_url: str | None,
    site: str,
    location: str | None = None,
    description: str | None = None,
    date_posted: str | None = None,
    is_remote: bool | None = None,
    min_amount: Any = None,
    max_amount: Any = None,
    interval: str | None = None,
) -> dict[str, Any]:
    """One posting in the shape `ingest.store()` already knows how to read."""
    return {
        "title": title,
        "company": company,
        # store() prefers job_url_direct, and for a board poll the direct URL is
        # all there is — there is no aggregator interstitial to fall back to.
        "job_url_direct": apply_url,
        "job_url": apply_url,
        "site": site,
        "location": location,
        "description": description,
        "date_posted": date_posted,
        "is_remote": is_remote,
        "min_amount": min_amount,
        "max_amount": max_amount,
        "interval": interval,
        "work_from_home_type": None,
    }


# ================================== greenhouse ==================================


def greenhouse(slug: str) -> list[dict[str, Any]]:
    """`content=true` returns the full JD in one call instead of one per job."""
    payload = _get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if jobs is None:
        raise BoardError(f"greenhouse/{slug}: no `jobs` array in the response")

    rows = []
    for job in jobs:
        offices = job.get("offices") or []
        location = (job.get("location") or {}).get("name") or ", ".join(
            office.get("name", "") for office in offices if office.get("name")
        )
        rows.append(
            _row(
                title=job.get("title"),
                # The board slug is a URL fragment, not a name — `nextinsurance66`
                # is a real one. The payload carries the display name; fall back
                # to the slug only when it does not.
                company=job.get("company_name") or slug,
                apply_url=job.get("absolute_url"),
                site="greenhouse",
                location=location or None,
                description=_plain(job.get("content")),
                date_posted=_iso(job.get("updated_at") or job.get("first_published")),
                is_remote="remote" in (location or "").lower(),
            )
        )
    return rows


# ===================================== lever =====================================


def lever(slug: str) -> list[dict[str, Any]]:
    postings = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(postings, list):
        raise BoardError(f"lever/{slug}: expected a list of postings")

    rows = []
    for job in postings:
        categories = job.get("categories") or {}
        workplace = (categories.get("commitment") or "") + " " + (
            job.get("workplaceType") or ""
        )
        salary = job.get("salaryRange") or {}
        rows.append(
            _row(
                title=job.get("text"),
                company=slug,
                # `applyUrl` goes straight to the form; `hostedUrl` is the
                # description page. Both dedup the same after normalization, but
                # the apply link is the one I actually want to open.
                apply_url=job.get("applyUrl") or job.get("hostedUrl"),
                site="lever",
                location=categories.get("location") or categories.get("allLocations"),
                description=job.get("descriptionPlain") or _plain(job.get("description")),
                date_posted=_iso(job.get("createdAt")),
                is_remote="remote" in workplace.lower(),
                min_amount=salary.get("min"),
                max_amount=salary.get("max"),
                interval=(salary.get("interval") or "").replace("per-year-salary", "yearly")
                or None,
            )
        )
    return rows


# ===================================== ashby =====================================


def ashby(slug: str) -> list[dict[str, Any]]:
    payload = _get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if jobs is None:
        raise BoardError(f"ashby/{slug}: no `jobs` array in the response")

    rows = []
    for job in jobs:
        # `isListed` false means the company pulled it from their public board.
        # Applying to one is a dead link, and the point of this project is that
        # every submitted application counts.
        if job.get("isListed") is False:
            continue
        comp = job.get("compensation") or {}
        summary = (comp.get("summaryComponents") or [{}])[0]
        rows.append(
            _row(
                title=job.get("title"),
                company=slug,
                apply_url=job.get("applyUrl") or job.get("jobUrl"),
                site="ashby",
                location=job.get("location"),
                description=job.get("descriptionPlain") or _plain(job.get("descriptionHtml")),
                date_posted=_iso(job.get("publishedAt") or job.get("updatedAt")),
                is_remote=bool(job.get("isRemote")),
                min_amount=summary.get("minValue"),
                max_amount=summary.get("maxValue"),
                interval=(summary.get("interval") or "").lower().replace("per year", "yearly")
                or None,
            )
        )
    return rows


# ============================== smartrecruiters ==============================


def smartrecruiters(slug: str) -> list[dict[str, Any]]:
    """Paginated, unlike the other three — 100 per page, `totalFound` up front.

    The list endpoint omits the JD. Fetching it costs one request per posting,
    which for a 500-req board is worse than the whole rest of the poll combined,
    so it is left out: `score` reads the title and location, and `tailor` only
    ever sees a JD I paste or a packet builds. Revisit if Phase 3 needs it.
    """
    rows: list[dict[str, Any]] = []
    offset, limit = 0, 100
    while True:
        payload = _get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?limit={limit}&offset={offset}"
        )
        page = payload.get("content") if isinstance(payload, dict) else None
        if page is None:
            raise BoardError(f"smartrecruiters/{slug}: no `content` array")
        for job in page:
            location = job.get("location") or {}
            city = location.get("city")
            region = location.get("region")
            where = ", ".join(part for part in (city, region) if part)
            rows.append(
                _row(
                    title=job.get("name"),
                    company=(job.get("company") or {}).get("name") or slug,
                    apply_url=f"https://jobs.smartrecruiters.com/{slug}/{job.get('id')}",
                    site="smartrecruiters",
                    location=where or None,
                    date_posted=_iso(job.get("releasedDate")),
                    is_remote=bool(location.get("remote")),
                )
            )
        offset += limit
        total = payload.get("totalFound") or 0
        # Bounded on purpose: a board with 10,000 postings is an agency, and
        # walking it is 100 requests against a host we are being polite to.
        if offset >= total or offset >= 500:
            break
    return rows


# ================================== rippling ==================================


def rippling(slug: str) -> list[dict[str, Any]]:
    """Thin payload — name, department, location, url. No JD, no dates."""
    postings = _get(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
    if not isinstance(postings, list):
        raise BoardError(f"rippling/{slug}: expected a list of postings")
    rows = []
    for job in postings:
        where = job.get("workLocation") or {}
        label = where.get("label") if isinstance(where, dict) else str(where)
        rows.append(
            _row(
                title=job.get("name"),
                company=slug,
                apply_url=job.get("url"),
                site="rippling",
                location=label,
                is_remote="remote" in (label or "").lower(),
            )
        )
    return rows


# ================================== workable ==================================


def workable(slug: str) -> list[dict[str, Any]]:
    payload = _get(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if jobs is None:
        raise BoardError(f"workable/{slug}: no `jobs` array")
    company = payload.get("name") or slug
    rows = []
    for job in jobs:
        rows.append(
            _row(
                title=job.get("title"),
                company=company,
                apply_url=job.get("application_url") or job.get("url") or job.get("shortlink"),
                site="workable",
                location=job.get("location") or job.get("city"),
                description=_plain(job.get("description")),
                date_posted=_iso(job.get("published_on") or job.get("created_at")),
                is_remote=bool(job.get("telecommuting")),
            )
        )
    return rows


# ================================== bamboohr ==================================


def bamboohr(slug: str) -> list[dict[str, Any]]:
    payload = _get(f"https://{slug}.bamboohr.com/careers/list")
    jobs = payload.get("result") if isinstance(payload, dict) else None
    if jobs is None:
        raise BoardError(f"bamboohr/{slug}: no `result` array")
    rows = []
    for job in jobs:
        location = job.get("location") or {}
        where = (
            ", ".join(
                str(part)
                for part in (location.get("city"), location.get("state"))
                if part
            )
            if isinstance(location, dict)
            else str(location)
        )
        rows.append(
            _row(
                title=job.get("jobOpeningName"),
                company=slug,
                apply_url=f"https://{slug}.bamboohr.com/careers/{job.get('id')}",
                site="bamboohr",
                location=where or job.get("atsLocation") or None,
                is_remote=bool(job.get("isRemote")),
            )
        )
    return rows


FETCHERS: dict[str, Callable[[str], list[dict[str, Any]]]] = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "rippling": rippling,
    "workable": workable,
    "bamboohr": bamboohr,
}


def fetch(vendor: str, slug: str) -> list[dict[str, Any]]:
    """One board. Raises `BoardError` for anything the caller should report."""
    fetcher = FETCHERS.get(vendor)
    if fetcher is None:
        raise BoardError(f"unknown board vendor {vendor!r}. Known: {sorted(FETCHERS)}")
    return fetcher(slug)
