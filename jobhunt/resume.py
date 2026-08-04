"""Corpus rows -> a RenderCV input document -> a PDF.

RenderCV 2.x exposes no public Python API — every `__init__.py` in the package is
empty — so this drives the `rendercv` CLI that ships in our own venv. That is the
documented interface; importing its internals would break on a point release.

Format is not a preference here. `.claude/rules/tailoring.md` requires single
column, standard headings, no tables or text boxes, no contact information in
page headers or footers, dates as `Jan 2023 - Mar 2025`, and real selectable
text. `_DESIGN` below is where each of those is pinned.

Filenames never carry a company name — `out/` is disposable but it is still on
disk, and employer names do not belong in filenames.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from . import config, queries

RENDERCV_BIN = Path(sys.executable).parent / "rendercv"

# Pinned to satisfy .claude/rules/tailoring.md. `engineeringresumes` is
# RenderCV's engineering/ATS preset: single column, black text, no icons, and
# section titles ruled across the page. `classic` — the previous choice — is the
# decorative theme, and it produced the layout defects listed below.
#
# Every template override here fixes one of them. RenderCV lays an entry out as
# two columns, and whatever goes in the right-hand one reserves width down the
# *whole* entry, not just its first line. So `LOCATION\nDATE`, the default, cost
# a quarter of every bullet's width to show a location nobody wants on a resume.
_DESIGN: dict[str, Any] = {
    "theme": "engineeringresumes",
    "page": {
        "size": "us-letter",
        "top_margin": "0.5in",
        "bottom_margin": "0.5in",
        "left_margin": "0.5in",
        "right_margin": "0.5in",
        "show_footer": False,
        "show_top_note": False,
    },
    "typography": {"alignment": "left", "font_size": {"body": "10pt"}},
    "links": {"underline": True, "show_external_link_icon": False},
    "sections": {
        # Without this an entry is an unbreakable block: a role with more bullets
        # than fit on a page gets pushed whole to the next one, leaving the
        # previous page blank and then overflowing with lines drawn on top of
        # each other. The master resume hits it immediately.
        "allow_page_break": True,
        # Kills the "1 year 8 months" line under every date range. It reads as
        # padding, and on a current role it re-dates the resume every month.
        "show_time_spans_in": [],
    },
    "entries": {"allow_page_break": True, "short_second_row": False},
    # One line per entry: bold primary, then the date right-aligned on the same
    # line. Matches the layout in oldProjects/cv-builder, which is the ATS shape
    # that has actually been used.
    "templates": {
        "experience_entry": {
            "main_column": "**COMPANY**, POSITION\nSUMMARY\nHIGHLIGHTS",
            "date_and_location_column": "DATE",
        },
        "normal_entry": {  # projects
            "main_column": "**NAME**\nSUMMARY\nHIGHLIGHTS",
            "date_and_location_column": "DATE",
        },
        "education_entry": {
            # `degree_column` put "Master of Science" in a narrow column of its
            # own, which wrapped to three lines and rendered as
            # "Master / State University, Computer Science / of / Science".
            "main_column": "**INSTITUTION**, DEGREE_WITH_AREA\nSUMMARY\nHIGHLIGHTS",
            "degree_column": None,
            "date_and_location_column": "DATE",
        },
    },
}

# `present` for an open-ended role, and `Jan`-style abbreviations, come from here.
_LOCALE: dict[str, Any] = {"language": "english", "present": "Present"}


class ResumeError(RuntimeError):
    """The corpus cannot produce a resume, or RenderCV refused to render one."""


# ================================ shaping data ================================


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _dates(start: str | None, end: str | None) -> dict[str, Any]:
    """`YYYY-MM` in, RenderCV date fields out. A NULL end month means current."""
    out: dict[str, Any] = {}
    if start:
        out["start_date"] = start
        out["end_date"] = end or "present"
    elif end:
        out["date"] = end
    return out


def _phone(raw: str) -> str:
    """RenderCV validates phone numbers and rejects the human-readable form.

    facts.yaml holds `555-123-4567` because that is what a person writes down;
    RenderCV wants E.164. Convert here rather than making the profile file store
    a format only one consumer cares about. Anything that is not plainly a US
    ten-digit number is passed through for RenderCV to judge and report.
    """
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        return text
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return text


def _social(facts: dict[str, str]) -> list[dict[str, str]]:
    """LinkedIn and GitHub want usernames, not URLs."""
    networks = []
    for key, network in (("identity.linkedin", "LinkedIn"), ("identity.github", "GitHub")):
        url = (facts.get(key) or "").rstrip("/")
        if url:
            networks.append({"network": network, "username": url.rsplit("/", 1)[-1]})
    return networks


def _header(facts: dict[str, str]) -> dict[str, Any]:
    """The resume header. `identity` in facts.yaml exists for exactly this."""
    name = facts.get("identity.preferred_name") or facts.get("identity.legal_name")
    if not name:
        raise ResumeError(
            "no name in profile_facts. Fill `identity` in docs/profile/facts.yaml "
            "and run `make load-profile`."
        )
    city, state = facts.get("identity.city"), facts.get("identity.state")
    header: dict[str, Any] = {"name": name}
    if city or state:
        header["location"] = ", ".join(part for part in (city, state) if part)
    for key, field in (
        ("identity.email", "email"),
        ("identity.website", "website"),
    ):
        if facts.get(key):
            header[field] = facts[key]
    if facts.get("identity.phone"):
        header["phone"] = _phone(facts["identity.phone"])
    social = _social(facts)
    if social:
        header["social_networks"] = social
    return header


def _highlights(
    rows: list[sqlite3.Row], selection: dict[int, str] | None
) -> list[str]:
    """Bullet text for one parent, in output order.

    With a selection, order follows the selection — the tailor is allowed to
    reorder — and the text is the tailor's rewording. Without one, this is the
    master resume: every bullet, in corpus order, verbatim.
    """
    if selection is None:
        return [row["text"] for row in rows]
    by_id = {int(row["id"]): row for row in rows}
    return [selection[bid] for bid in selection if bid in by_id]


# The Skills line is the one section with no natural length limit, and it was
# the reason a tailored resume ran to two pages: ~50 entries, eleven lines,
# more space than any single role. Truncation is safe because the ordering below
# is strongest-claim-first, so what survives is what I actually built.
MAX_TECHNOLOGIES = 22


def _tech_union(rows: list[sqlite3.Row], *, limit: int = MAX_TECHNOLOGIES) -> list[str]:
    """Technologies across the corpus, deduped, strongest claim first.

    Ordered built -> owned -> maintained -> touched so that first use wins, but
    emitted as one line: `built` versus `touched` is the corpus's own vocabulary
    for how well I know something, and it is not a claim a resume should make.

    Nothing here decides *relevance* to a given job — the ordering is by how well
    I know a thing, not by what the posting asked for. Picking per-JD would mean
    letting the tailor choose, which is a Phase 3 change to the prompt contract.
    """
    seen: dict[str, None] = {}
    for bucket in ("tech_built", "tech_owned", "tech_maintained", "tech_touched"):
        for row in rows:
            if bucket in row.keys():
                for item in _json_list(row[bucket]):
                    seen.setdefault(item.strip(), None)
    return [item for item in seen if item][:limit]


# ================================ the document ================================


def build_cv(
    conn: sqlite3.Connection, *, selection: dict[int, str] | None = None
) -> dict[str, Any]:
    """Build the RenderCV `cv` mapping from corpus rows.

    `selection` maps `bullets.id` to the text that should appear for it, in
    insertion order. It is the tailor's output, already validated. `None` builds
    the master resume: everything, verbatim.
    """
    facts = queries.profile_facts(conn)
    experiences = queries.corpus_experiences(conn)
    projects = queries.corpus_projects(conn)
    bullets = queries.corpus_bullets(conn)

    if selection is not None:
        known = {int(row["id"]) for row in bullets}
        unknown = [bid for bid in selection if bid not in known]
        if unknown:
            raise ResumeError(f"selection names bullets that are not in the corpus: {unknown}")

    by_experience: dict[int, list[sqlite3.Row]] = {}
    by_project: dict[int, list[sqlite3.Row]] = {}
    for row in bullets:
        if row["experience_id"] is not None:
            by_experience.setdefault(int(row["experience_id"]), []).append(row)
        else:
            by_project.setdefault(int(row["project_id"]), []).append(row)

    sections: dict[str, list[Any]] = {}

    experience_entries = []
    for row in experiences:
        highlights = _highlights(by_experience.get(int(row["id"]), []), selection)
        if selection is not None and not highlights:
            continue  # nothing selected from this role
        entry: dict[str, Any] = {
            "company": row["company"],
            "position": row["title"],
            **_dates(row["start_month"], row["end_month"]),
        }
        if row["location"]:
            entry["location"] = row["location"]
        if highlights:
            entry["highlights"] = highlights
        experience_entries.append(entry)
    if experience_entries:
        sections["experience"] = experience_entries

    project_entries = []
    for row in projects:
        highlights = _highlights(by_project.get(int(row["id"]), []), selection)
        if selection is not None and not highlights:
            continue
        name = row["name"]
        if row["url"]:
            name = f"[{name}]({row['url']})"  # markdown link, per RenderCV
        entry = {"name": name, **_dates(row["start_month"], row["end_month"])}
        # `blurb` is deliberately not emitted as `summary`. RenderCV renders a
        # summary as an unbulleted paragraph above the bullets, which is what
        # made the ARC entry read as a wall of text with no bullet marker, and
        # on a one-page resume a paragraph per project is the whole page. The
        # blurb stays in the corpus — it is what the tailor reads for context.
        if highlights:
            entry["highlights"] = highlights
        project_entries.append(entry)
    if project_entries:
        sections["projects"] = project_entries

    education_entries = []
    for row in queries.corpus_education(conn):
        entry = {"institution": row["school"], **_dates(row["start_month"], row["end_month"])}
        if row["degree"]:
            entry["degree"] = row["degree"]
        if row["field"]:
            entry["area"] = row["field"]
        if row["notes"]:
            entry["highlights"] = [row["notes"]]
        education_entries.append(entry)
    if education_entries:
        sections["education"] = education_entries

    # Scoped to what this resume actually shows. On a tailored resume the union
    # across the whole corpus is a ~50-item wall that crowds out the bullets, and
    # foregrounding the relevant skills is explicitly permitted.
    if selection is None:
        tech_sources = list(experiences) + list(projects)
    else:
        tech_sources = [r for r in experiences if by_experience.get(int(r["id"])) and _highlights(by_experience[int(r["id"])], selection)]
        tech_sources += [r for r in projects if by_project.get(int(r["id"])) and _highlights(by_project[int(r["id"])], selection)]
    technologies = _tech_union(tech_sources)
    if technologies:
        sections["skills"] = [{"label": "Technologies", "details": ", ".join(technologies)}]

    # No `languages` section. Spoken languages are not a hiring signal for these
    # roles and the line cost a section heading to say "Chinese, English".
    # `corpus_languages` still loads them; nothing on the resume reads it.

    credentials: dict[str, list[Any]] = {}
    for row in queries.corpus_credentials(conn):
        heading = _CREDENTIAL_SECTIONS.get(row["kind"])
        if heading is None:  # kind not shown on a resume — see below
            continue
        detail = " · ".join(part for part in (row["issuer"], row["issued"]) if part)
        credentials.setdefault(heading, []).append(
            {"label": row["title"], "details": detail}
        )
    sections.update(credentials)

    if not sections:
        raise ResumeError("the corpus is empty. Run `make load-profile` first.")
    return {**_header(facts), "sections": _ordered(sections)}


# RenderCV renders sections in the order the mapping gives them, so until now the
# order was whichever order `build_cv` happened to write them in — skills last,
# because that is where the code that builds it sits. This makes it a decision.
# Skills lead, as in oldProjects/cv-builder: it is the section a keyword screen
# reads first, and it is the cheapest one for a human to skim.
_SECTION_ORDER = ("skills", "experience", "projects", "education")


def _ordered(sections: dict[str, list[Any]]) -> dict[str, list[Any]]:
    """`sections`, in resume order. Anything unlisted keeps its position, last."""
    known = [name for name in _SECTION_ORDER if name in sections]
    rest = [name for name in sections if name not in _SECTION_ORDER]
    return {name: sections[name] for name in (*known, *rest)}


# Credential kinds that earn a section on the resume. `poster` is deliberately
# absent: the one poster in the corpus is the ARC/PaperPrism presentation, and
# the work itself is already a project entry with bullets, so the section
# restated it under a heading of its own. Everything here is a one-line change
# if that judgement turns out wrong — a kind missing from this map is skipped,
# not crashed on.
_CREDENTIAL_SECTIONS = {
    "cert": "certifications",
    "license": "licenses",
    "publication": "publications",
    "talk": "talks",
    "award": "awards",
    "patent": "patents",
}


def build_document(
    conn: sqlite3.Connection, *, selection: dict[int, str] | None = None
) -> dict[str, Any]:
    """The complete RenderCV input: content plus the pinned format."""
    return {
        "cv": build_cv(conn, selection=selection),
        "design": _DESIGN,
        "locale": _LOCALE,
        "settings": {
            "render_command": {
                # PDF only. The PNG and HTML passes double render time and
                # nothing downstream reads them.
                "dont_generate_png": True,
                "dont_generate_html": True,
                "dont_generate_markdown": True,
                "dont_generate_typst": False,
            }
        },
    }


# ================================= rendering =================================


def render(document: dict[str, Any], out_path: Path) -> Path:
    """Write `document`, run RenderCV, and return the PDF path.

    Raises rather than returning a partial result: a packet with no resume is a
    failure the dashboard has to see, not something to paper over.
    """
    if not RENDERCV_BIN.exists():
        raise ResumeError(
            f"rendercv is not installed at {RENDERCV_BIN}. "
            "Install it with: uv pip install -e '.[resume]'"
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / f".{out_path.stem}_build"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    source = work / "cv.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))

    result = subprocess.run(
        # Not --quiet: RenderCV reports validation errors on stdout, and those
        # are exactly what has to reach the caller when a render fails.
        [str(RENDERCV_BIN), "render", str(source), "--output-folder", "output"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    produced = sorted((work / "output").glob("*.pdf")) if (work / "output").exists() else []
    if result.returncode != 0 or not produced:
        noise = re.compile(r"\x1b\[[0-9;]*m")
        detail = noise.sub("", result.stdout + result.stderr).strip()[-1500:]
        raise ResumeError(f"rendercv failed (exit {result.returncode}):\n{detail}")

    shutil.move(str(produced[0]), out_path)
    shutil.rmtree(work, ignore_errors=True)
    return out_path


def render_master(conn: sqlite3.Connection, out_path: Path | None = None) -> Path:
    """The untailored resume: every bullet in the corpus, verbatim.

    This is the baseline every tailored resume is diffed against.
    """
    target = Path(out_path) if out_path else config.OUT_DIR / "master.pdf"
    return render(build_document(conn), target)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render the master resume to PDF.")
    parser.add_argument("--out", default=None, help="output path (default out/master.pdf)")
    args = parser.parse_args(argv)

    from .db import connect

    conn = connect()
    try:
        path = render_master(conn, Path(args.out) if args.out else None)
    except ResumeError as exc:
        print(f"resume not rendered: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"{path} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
