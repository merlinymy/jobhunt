"""Table-driven fixtures for URL normalization and ATS detection.

Two of the three tests this project permits (CLAUDE.md, "No test suite"). Both
exist because dedup correctness lives in `normalize.py` and nothing else checks
it: `jobs.apply_url_norm` is UNIQUE, so a key that comes out wrong does not
error — it tracks one posting twice, or collapses two postings into one.

Every URL below is real, harvested from a live `make ingest` sweep on
2026-08-03 rather than invented. That matters: the sweep is what revealed that
`grnh.se` (Greenhouse's shortener, the single commonest apply host Indeed
returns), `jobs.smartrecruiters.com`, `wd5.myworkdaysite.com`, and
`phh.tbe.taleo.net` all fell through the patterns and were being filed as
"direct" on the conversion-by-ATS table.

Run it directly; no pytest required:

    .venv/bin/python tests/test_url_normalization.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt.normalize import (  # noqa: E402
    UnparseableURL,
    detect_ats,
    is_redirect_wrapper,
    normalize_apply_url,
)

# --------------------------------------------------------------------------
# normalize_apply_url — the hard dedup key.
# (input, expected)
# --------------------------------------------------------------------------

NORMALIZATION: list[tuple[str, str]] = [
    # Tracking params come off. Each of these was on a real posting, and each
    # would otherwise make the same job look new every time its source changed.
    (
        "https://jobs.lever.co/zoox/4d58c75b-57a0-43f9-860c-2d4b146b299e?lever-source=Indeed",
        "https://jobs.lever.co/zoox/4d58c75b-57a0-43f9-860c-2d4b146b299e",
    ),
    # Path case is preserved — see the JPMorganChase case below for why. Ashby
    # boards really are served with a capitalised slug.
    (
        "https://jobs.ashbyhq.com/Clera/04a55796-a954-4c4f-acbf-8ad44f645a37?utm_source=6qJKzkB7bV",
        "https://jobs.ashbyhq.com/Clera/04a55796-a954-4c4f-acbf-8ad44f645a37",
    ),
    (
        "https://ats.rippling.com/opiniion/jobs/1b26e1c2-662a-40b2-a9d2-95d3f14fe2e3?jobSite=Indeed",
        "https://ats.rippling.com/opiniion/jobs/1b26e1c2-662a-40b2-a9d2-95d3f14fe2e3?jobSite=Indeed",
    ),
    # `gh_jid` identifies the posting, so it must survive. Dropping it would
    # collapse every Greenhouse job on one board into a single key.
    (
        "https://boards.greenhouse.io/crunchyroll/jobs/8094802?gh_jid=8094802",
        "https://boards.greenhouse.io/crunchyroll/jobs/8094802?gh_jid=8094802",
    ),
    # http -> https, and `www.` comes off.
    (
        "http://www.indeed.com/job/software-engineer-12562dc9cddd35de",
        "https://indeed.com/job/software-engineer-12562dc9cddd35de",
    ),
    # Host lowercases; the path does not, or two distinct postings could merge.
    (
        "https://JPMorganChase.contacthr.com/152917710",
        "https://jpmorganchase.contacthr.com/152917710",
    ),
    # Trailing slash and fragment go.
    (
        "https://careers.caterpillar.com/en/jobs/r0000386125/lead-software-engineer-gen-ai/",
        "https://careers.caterpillar.com/en/jobs/r0000386125/lead-software-engineer-gen-ai",
    ),
    (
        "https://nexamp.com//careers?gh_jid=8660665002#Open%20Roles",
        "https://nexamp.com//careers?gh_jid=8660665002",
    ),
    # utm_* prefix rule, several at once.
    (
        "https://careers.adobe.com/us/en/job/ADOBUSR170673EXTERNALENUS/Software-Development-Engineer"
        "?utm_source=indeed&utm_medium=phenom-feeds",
        "https://careers.adobe.com/us/en/job/ADOBUSR170673EXTERNALENUS/Software-Development-Engineer",
    ),
    # Param order must not produce two keys for one posting.
    (
        "https://example.com/job?b=2&a=1",
        "https://example.com/job?a=1&b=2",
    ),
    ("https://example.com/job?a=1&b=2", "https://example.com/job?a=1&b=2"),
    # Bare host, no scheme.
    ("boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/1"),
    # Default ports are noise; a real one is part of the address.
    ("https://example.com:443/job/1", "https://example.com/job/1"),
    ("http://example.com:80/job/1", "https://example.com/job/1"),
    ("https://example.com:8080/job/1", "https://example.com:8080/job/1"),
]

# Must raise rather than invent a key — a mangled key tracks a posting twice.
#
# Deliberately short. Raising means ingest *drops the posting*, and CLAUDE.md is
# explicit that nothing may discard a shot over unreliable data — so the bar is
# "urlsplit cannot produce an address at all", not "this URL looks wrong". A
# space in the host parses fine and yields a stable key, so it is allowed
# through even though the link is broken; a bad port or an unterminated IPv6
# literal cannot yield an address at all.
UNPARSEABLE: list[str] = [
    "http://x:99999/y",  # port out of range
    "http://[::1/job",   # unterminated IPv6 literal
]

# --------------------------------------------------------------------------
# detect_ats — read-time derivation, feeds conversion-by-ATS on the stats page.
# (url, expected_type, expected_slug)
# --------------------------------------------------------------------------

ATS: list[tuple[str, str | None, str | None]] = [
    # The four that a live sweep proved were being missed.
    ("https://grnh.se/utiep3gg5us", "greenhouse", None),
    ("https://jobs.smartrecruiters.com/ServiceNow/744000141345928-staff", "smartrecruiters", "servicenow"),
    ("https://wd5.myworkdaysite.com/recruiting/livingspaces/LS/job/x", "workday", "livingspaces"),
    ("https://phh.tbe.taleo.net/phh03/ats/careers/v2/viewRequisition?org=MILCORP", "taleo", "phh"),
    # The ones that already worked, kept so a regex edit cannot quietly break them.
    ("https://boards.greenhouse.io/crunchyroll/jobs/8094802", "greenhouse", "crunchyroll"),
    ("https://job-boards.greenhouse.io/acme/jobs/12345", "greenhouse", "acme"),
    ("https://jobs.lever.co/zoox/4d58c75b", "lever", "zoox"),
    ("https://jobs.ashbyhq.com/pariveda/cc4fc0be", "ashby", "pariveda"),
    ("https://apply.workable.com/j/8333E720D4", "workable", "j"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x", "workday", "nvidia"),
    ("https://chicago.taleo.net/careersection/100/jobdetail.ftl?job=203342", "taleo", "chicago"),
    ("https://acme.icims.com/jobs/1234/job", "icims", "acme"),
    # Added from the sweep.
    ("https://eklm.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/33203", "oracle", None),
    ("https://fa-ertb-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/x/requisitions/preview/26605", "oracle", None),
    ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html", "adp", None),
    ("https://recruiting.adp.com/srccsh/public/RTI.home?r=5001215900106", "adp", None),
    ("https://recruitingbypaycor.com/career/CareerHome.action?clientId=8a78", "paycor", None),
    ("https://dockyard.bamboohr.com/careers/29", "bamboohr", "dockyard"),
    ("https://app.jobvite.com/CompanyJobs/Job.aspx?j=o7xAAfwy", "jobvite", None),
    ("https://ats.rippling.com/opiniion/jobs/1b26e1c2", "rippling", "opiniion"),
    ("https://belaytechnologies.applytojob.com/apply/1eDwLcUVZi/x", "jazzhr", "belaytechnologies"),
    ("https://recruiting.paylocity.com/Recruiting/Jobs/Details/4382401/x", "paylocity", None),
    ("https://inmusicbrands.pinpointhq.com/postings/863ed9fb", "pinpoint", "inmusicbrands"),
    ("https://smart-tech-skills.careerplug.com/j/032rw1a", "careerplug", "smart-tech-skills"),
    ("https://deloitteus.avature.net/careers/InviteToApply?jobId=361253", "avature", "deloitteus"),
    ("https://morganstanley.eightfold.ai/careers/job?pid=549797103564", "eightfold", "morganstanley"),
    ("https://scig.oorwin.ai/#/careers/index.html/job/details/1ecaeb", "oorwin", "scig"),
    ("https://app.trinethire.com/companies/294644-betterworks/jobs/123132", "trinet", None),
    ("https://www.applicantpro.com/openings/priscorp/jobs/4163552", "applicantpro", None),
    ("https://www1.jobdiva.com/candidates/myjobs/openjob_outside.jsp?a=iyj", "jobdiva", None),
    ("https://candidateportal.ceipal.com/job-details/VoLnfT5WOACKbyH26UogMR9", "ceipal", None),
    # Not an ATS. A company's own careers page and an aggregator redirect both
    # belong in the "direct / other" bucket, and neither may crash the page.
    ("https://www.amazon.jobs/jobs/10490378/software-development-engineer", None, None),
    ("https://careers.google.com/jobs/results/115178291576349382-software", None, None),
    ("https://click.appcast.io/t/KWblGc9n_G3lzvI0Y0IPlQyEwCZkzvP4C7P-6EvdvOM=", None, None),
    ("not a url at all", None, None),
    ("http://x:99999/y", None, None),  # unparseable must not raise here
]

# Aggregator redirect wrappers — a dedup hazard, not an ATS.
REDIRECTS: list[tuple[str, bool]] = [
    ("https://click.appcast.io/t/KWblGc9n_G3lzvI0Y0IPlQyEwCZkzvP4C7P-6EvdvOM=", True),
    ("https://jsv3.recruitics.com/redirect?rx_cid=3426&rx_jobId=01849177_rxr-1", True),
    ("https://rr.jobsyn.org/17B5B13A3CB740E293C0B3EB2E510AC01554", True),
    ("https://dsp.prng.co/oHSb2hb", True),
    ("https://tnl2.jometer.com/v2/job?jz=5wqzs7a993af6a8", True),
    ("https://jobs.lever.co/zoox/4d58c75b", False),
    ("https://grnh.se/utiep3gg5us", False),
]


def run() -> int:
    failures: list[str] = []

    print("normalize_apply_url")
    for raw, expected in NORMALIZATION:
        try:
            got = normalize_apply_url(raw)
        except UnparseableURL as exc:
            failures.append(f"raised on {raw!r}: {exc}")
            print(f"  FAIL  raised: {raw[:70]}")
            continue
        if got == expected:
            print(f"  ok    {raw[:66]}")
        else:
            failures.append(f"{raw!r}\n      expected {expected!r}\n      got      {got!r}")
            print(f"  FAIL  {raw[:66]}\n          expected {expected}\n          got      {got}")

    print("\nnormalize_apply_url — must raise")
    for raw in UNPARSEABLE:
        try:
            got = normalize_apply_url(raw)
        except UnparseableURL:
            print(f"  ok    rejected {raw!r}")
        else:
            failures.append(f"NOT REJECTED: {raw!r} -> {got!r}")
            print(f"  FAIL  accepted {raw!r} -> {got!r}")

    print("\ndetect_ats")
    for raw, want_type, want_slug in ATS:
        got_type, got_slug = detect_ats(raw)
        if (got_type, got_slug) == (want_type, want_slug):
            print(f"  ok    {str(want_type):16} {raw[:56]}")
        else:
            failures.append(
                f"{raw!r}\n      expected {(want_type, want_slug)!r}\n      got      {(got_type, got_slug)!r}"
            )
            print(f"  FAIL  {raw[:56]}\n          expected {(want_type, want_slug)}\n          got      {(got_type, got_slug)}")

    print("\nis_redirect_wrapper")
    for raw, want in REDIRECTS:
        got = is_redirect_wrapper(raw)
        if got == want:
            print(f"  ok    {str(want):5} {raw[:60]}")
        else:
            failures.append(f"is_redirect_wrapper({raw!r}) = {got}, expected {want}")
            print(f"  FAIL  {raw[:60]} -> {got}, expected {want}")

    total = len(NORMALIZATION) + len(UNPARSEABLE) + len(ATS) + len(REDIRECTS)
    print(f"\n{total - len(failures)}/{total} cases behaved correctly")
    for line in failures:
        print(f"  {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
