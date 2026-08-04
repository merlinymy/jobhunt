"""Normalization and derivation. Dedup correctness lives here.

`normalize_apply_url` is the hard dedup key generator; `detect_ats` is derived
on read from the apply URL and is never stored as a prerequisite for anything.
Both are on the short list of things worth table-driven tests against real URLs.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Source tags, not job identifiers. Anything that could identify a posting stays.
TRACKING_PARAMS = frozenset(
    {"gh_src", "ref", "source", "lever-source", "src", "trk", "recruiter"}
)
TRACKING_PREFIXES = ("utm_",)

# Ordered: first match wins. Keyed on host, slug pulled from host or path.
#
# Every pattern below the first block was added from real apply URLs that came
# back from a live ingest sweep, after 9 of 25 postings bucketed as "direct".
# That number is not cosmetic: the stats page reports conversion by ATS, so a
# misfiled Greenhouse posting silently makes Greenhouse look like it converts
# worse than it does. Re-check this list whenever the "direct" share climbs.
ATS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(r"^(?:job-boards|boards)\.greenhouse\.io/([^/]+)")),
    # Greenhouse's own shortener, and the commonest form Indeed hands back —
    # 6 of the first 25 postings. The slug is the posting, not the company, so
    # there is no company slug to pull out of it.
    ("greenhouse", re.compile(r"^grnh\.se/")),
    ("lever", re.compile(r"^jobs\.lever\.co/([^/]+)")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com/([^/]+)")),
    ("workable", re.compile(r"^apply\.workable\.com/([^/]+)")),
    ("workday", re.compile(r"^([^.]+)\.(?:wd\d+\.)?myworkdayjobs\.com")),
    # Workday's other host. Here the tenant is in the path, not the subdomain:
    # `wd5.myworkdaysite.com/recruiting/livingspaces/...`.
    ("workday", re.compile(r"^wd\d*\.myworkdaysite\.com/recruiting/([^/]+)")),
    # `([^.]+)\.taleo\.net` only matched a single label, so the real-world
    # `phh.tbe.taleo.net` — Taleo's Business Edition, which is most of them —
    # missed entirely. Anchor on the tail and take the first label as the slug.
    ("taleo", re.compile(r"^([^.]+)(?:\.[^.]+)*\.taleo\.net")),
    # `careers.` only, until a live sweep returned `jobs.smartrecruiters.com`.
    ("smartrecruiters", re.compile(r"^(?:careers|jobs)\.smartrecruiters\.com/([^/]+)")),
    ("icims", re.compile(r"^([^.]+)\.icims\.com")),
    # Oracle Recruiting Cloud. Two host shapes in one sweep
    # (`eklm.fa.us2.oraclecloud.com`, `fa-ertb-saasfaprod1.fa.ocs.oraclecloud.com`),
    # and the first label is a tenant id rather than a company name, so no slug.
    ("oracle", re.compile(r"^[^/]*\.oraclecloud\.com")),
    ("adp", re.compile(r"^(?:workforcenow|recruiting)\.adp\.com")),
    ("paycor", re.compile(r"^(?:recruiting\.)?(?:recruitingbypaycor|paycor)\.com")),
    ("bamboohr", re.compile(r"^([^.]+)\.bamboohr\.com")),
    # The real host is `app.jobvite.com`; `jobs.jobvite.com` never appeared.
    ("jobvite", re.compile(r"^(?:app|jobs)\.jobvite\.com")),
    ("breezy", re.compile(r"^([^.]+)\.breezy\.hr")),
    ("rippling", re.compile(r"^ats\.rippling\.com/([^/]+)")),
    ("jazzhr", re.compile(r"^([^.]+)\.applytojob\.com")),
    ("paylocity", re.compile(r"^recruiting\.paylocity\.com")),
    ("pinpoint", re.compile(r"^([^.]+)\.pinpointhq\.com")),
    ("careerplug", re.compile(r"^([^.]+)\.careerplug\.com")),
    ("avature", re.compile(r"^([^.]+)\.avature\.net")),
    ("eightfold", re.compile(r"^([^.]+)\.eightfold\.ai")),
    ("oorwin", re.compile(r"^([^.]+)\.oorwin\.ai")),
    ("trinet", re.compile(r"^app\.trinethire\.com")),
    ("applicantpro", re.compile(r"^(?:www\.)?applicantpro\.com")),
    ("jobdiva", re.compile(r"^www\d*\.jobdiva\.com")),
    ("ceipal", re.compile(r"^candidateportal\.ceipal\.com")),
)

# Aggregator redirect wrappers. Not an ATS — the real apply URL is behind a
# token, so these are a *dedup* problem: one posting reached through a wrapper
# and again directly produces two `apply_url_norm` keys and gets tracked twice.
# Recognised here so the size of that problem is visible rather than hiding in
# the "direct" bucket on the stats page. `recruitics` carries the real URL in
# its `rx_url` query param and could be unwrapped; the rest are opaque. Both are
# the "soft dedup" line item in docs/build-plan.md Phase 4, not solved here.
REDIRECT_HOSTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^click\.appcast\.io/"),
    re.compile(r"^jsv\d*\.recruitics\.com/"),
    re.compile(r"^rr\.jobsyn\.org/"),
    re.compile(r"^dsp\.prng\.co/"),
    re.compile(r"^tnl\d*\.jometer\.com/"),
)


def is_redirect_wrapper(apply_url: str) -> bool:
    """True if the URL is an aggregator redirect rather than a real apply page."""
    try:
        parts = urlsplit(apply_url if "//" in apply_url else f"https://{apply_url}")
        target = f"{(parts.hostname or '').lower().removeprefix('www.')}{parts.path}"
    except ValueError:
        return False
    return any(pattern.match(target) for pattern in REDIRECT_HOSTS)


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


class UnparseableURL(ValueError):
    """An apply URL that `urlsplit` cannot make sense of."""


def normalize_apply_url(url: str) -> str:
    """Produce the hard dedup key for `jobs.apply_url_norm`.

    Lowercases scheme and host, drops `www.`, forces https, strips the trailing
    slash and the fragment, and drops known source tags. Surviving params are
    sorted so param order can't produce two keys for one posting.

    Raises `UnparseableURL` rather than inventing a key. This is the dedup guard —
    a silently mangled key is worse than a rejected paste, because it lets one
    posting be tracked twice.
    """
    try:
        parts = urlsplit(url.strip())
        if not parts.netloc:  # bare "example.com/jobs/1" parses as all-path
            parts = urlsplit(f"https://{url.strip()}")

        host = parts.hostname or ""
        host = host.removeprefix("www.")
        # .port itself raises on a non-numeric or out-of-range port.
        port = parts.port
    except ValueError as exc:
        raise UnparseableURL(f"can't parse that apply URL ({exc}): {url.strip()!r}") from exc

    if port and port not in (80, 443):
        host = f"{host}:{port}"

    path = parts.path.rstrip("/")
    kept = sorted(
        (key, value) for key, value in parse_qsl(parts.query) if not _is_tracking(key)
    )
    return urlunsplit(("https", host, path, urlencode(kept), ""))


def detect_ats(apply_url: str) -> tuple[str | None, str | None]:
    """Return `(ats_type, ats_slug)` derived from the apply URL.

    Total by design: this runs on every row of the index and the stats tables, so
    one unparseable URL must not take those pages down. Unrecognized and
    unparseable both mean "bucket it as other / direct".
    """
    try:
        parts = urlsplit(apply_url if "//" in apply_url else f"https://{apply_url}")
        host = (parts.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return None, None
    target = f"{host}{parts.path}"
    for name, pattern in ATS_PATTERNS:
        match = pattern.match(target)
        if match:
            # Some hosts carry no company slug at all — a Greenhouse short link
            # names the posting, an Oracle tenant id names nothing. Knowing the
            # ATS is the point; the slug is a bonus, so its absence is None
            # rather than a reason to report no match.
            slug = match.group(1).lower() if match.groups() and match.group(1) else None
            return name, slug
    return None, None


_PUNCT = re.compile(r"[^a-z0-9]+")
# Suffixes that differ between an aggregator's spelling and a careers page's.
_COMPANY_SUFFIXES = ("inc", "llc", "ltd", "corp", "corporation", "co", "gmbh", "plc")


def norm_company_name(name: str) -> str:
    """Dedup key for `companies.name_norm`."""
    words = _PUNCT.sub(" ", name.lower()).split()
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def norm_title(title: str) -> str:
    """Soft-dedup key for `jobs.title_norm` — same role reposted or re-listed."""
    text = _PUNCT.sub(" ", title.lower())
    # Drop req IDs and the roman-numeral / roman-ish level suffixes aggregators add.
    text = re.sub(r"\b(?:req|job|id)\s*\d+\b", " ", text)
    return " ".join(text.split())
