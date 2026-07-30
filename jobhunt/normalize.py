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
ATS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(r"^(?:job-boards|boards)\.greenhouse\.io/([^/]+)")),
    ("lever", re.compile(r"^jobs\.lever\.co/([^/]+)")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com/([^/]+)")),
    ("workable", re.compile(r"^apply\.workable\.com/([^/]+)")),
    ("workday", re.compile(r"^([^.]+)\.(?:wd\d+\.)?myworkdayjobs\.com")),
    ("taleo", re.compile(r"^([^.]+)\.taleo\.net")),
    ("smartrecruiters", re.compile(r"^careers\.smartrecruiters\.com/([^/]+)")),
    ("icims", re.compile(r"^([^.]+)\.icims\.com")),
)


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def normalize_apply_url(url: str) -> str:
    """Produce the hard dedup key for `jobs.apply_url_norm`.

    Lowercases scheme and host, drops `www.`, forces https, strips the trailing
    slash and the fragment, and drops known source tags. Surviving params are
    sorted so param order can't produce two keys for one posting.
    """
    parts = urlsplit(url.strip())
    if not parts.netloc:  # bare "example.com/jobs/1" parses as all-path
        parts = urlsplit(f"https://{url.strip()}")

    host = parts.hostname or ""
    host = host.removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    path = parts.path.rstrip("/")
    kept = sorted(
        (key, value) for key, value in parse_qsl(parts.query) if not _is_tracking(key)
    )
    return urlunsplit(("https", host, path, urlencode(kept), ""))


def detect_ats(apply_url: str) -> tuple[str | None, str | None]:
    """Return `(ats_type, ats_slug)` derived from the apply URL."""
    parts = urlsplit(apply_url if "//" in apply_url else f"https://{apply_url}")
    host = (parts.hostname or "").lower().removeprefix("www.")
    target = f"{host}{parts.path}"
    for name, pattern in ATS_PATTERNS:
        match = pattern.match(target)
        if match:
            return name, match.group(1).lower()
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
