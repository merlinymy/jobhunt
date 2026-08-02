"""Import docs/profile/* into SQLite. Idempotent, re-runnable.

`docs/profile/` is the source of truth and the DB is derived, so this only ever
runs one way: files in, rows out. There is no CRUD editor and there will not be
one — editing the files is the interface.

Four rules from .claude/rules/data-layer.md shape everything here:

  * Upserts, never duplicates. Re-running on unchanged files is a no-op.
  * Safe to run mid-fill. A blank or missing key is skipped, not written as an
    empty string over something real. Implemented with COALESCE on every
    optional column, so a half-filled file can be loaded without damage.
  * Rows the YAML no longer mentions survive unless `--prune` is passed.
  * Malformed YAML fails loudly instead of importing half the corpus.

Output is deliberately counts-only. Employer names do not belong in log output.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

from . import config
from .db import connect, transaction

FACTS_FILE = "facts.yaml"
EXPERIENCE_FILE = "experience.yaml"


class ProfileError(RuntimeError):
    """The profile files are unreadable, malformed, or the wrong shape."""


# ================================== reading ==================================


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ProfileError(f"{path} is missing. See docs/intake.md.")
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path.name} is not valid YAML: {exc}") from exc


def _expect_list(value: Any, path: Path, key: str) -> list[dict[str, Any]]:
    """A top-level section must be a list of mappings, or absent."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProfileError(f"{path.name}: `{key}` should be a list, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ProfileError(
                f"{path.name}: `{key}[{index}]` should be a mapping, got {type(item).__name__}"
            )
    return value


def _blank(value: Any) -> bool:
    """Missing, empty, or whitespace — all mean "not filled in yet"."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _text(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    return None if _blank(value) else str(value).strip()


def _json_list(row: dict[str, Any], key: str) -> str | None:
    """Multi-valued YAML field -> JSON array, matching `bullets.skills`."""
    value = row.get(key)
    if _blank(value):
        return None
    if not isinstance(value, list):
        raise ProfileError(f"`{key}` should be a list, got {type(value).__name__}")
    kept = [str(item).strip() for item in value if not _blank(item)]
    return json.dumps(kept) if kept else None


def _tech(row: dict[str, Any], bucket: str) -> str | None:
    tech = row.get("tech")
    if not isinstance(tech, dict):
        return None
    return _json_list(tech, bucket)


def _required(row: dict[str, Any], key: str, where: str) -> str:
    value = _text(row, key)
    if value is None:
        raise ProfileError(f"{where}: `{key}` is required and is empty")
    return value


# ================================= flattening =================================


def _flatten_facts(node: Any, prefix: str = "") -> dict[str, str]:
    """facts.yaml -> `profile_facts` key/value pairs.

    Keys keep their YAML path (`identity.email`, `comp.walk_away`) rather than
    being flattened to bare names: the path is where the value came from, and
    two sections could otherwise collide on a short name.
    """
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_flatten_facts(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        if not _blank(node):
            out[prefix] = json.dumps([str(item).strip() for item in node])
    elif not _blank(node):
        out[prefix] = str(node).strip()
    return out


# ================================== writing ==================================


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    keys: dict[str, Any],
    values: dict[str, Any],
    conflict: str,
) -> int:
    """Insert or update one row, then return its id.

    Optional columns update through COALESCE(excluded.x, x): a value the file
    does not carry yet leaves whatever is already stored alone. That is what
    makes this safe to run against a half-filled file.
    """
    columns = {**keys, **values}
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{name} = COALESCE(excluded.{name}, {name})" for name in values)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT {conflict} DO UPDATE SET {updates}"
        if values
        else f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT {conflict} DO NOTHING"
    )
    conn.execute(sql, tuple(columns.values()))

    where = " AND ".join(
        f"{name} IS ?" if value is None else f"{name} = ?" for name, value in keys.items()
    )
    row = conn.execute(
        f"SELECT id FROM {table} WHERE {where}", tuple(keys.values())
    ).fetchone()
    if row is None:  # pragma: no cover - would mean the conflict target is wrong
        raise ProfileError(f"{table}: upsert did not produce a row for {list(keys)}")
    return int(row["id"])


def _load_bullets(
    conn: sqlite3.Connection,
    parent_column: str,
    parent_id: int,
    bullets: list[dict[str, Any]],
    where: str,
) -> int:
    """Bullets are identified by position under their parent.

    Editing the text of the third bullet updates the third row rather than
    orphaning it and appending a fourth — which keeps `bullets.id` stable, and
    those IDs are what the tailor selects by.
    """
    other = "project_id" if parent_column == "experience_id" else "experience_id"
    for index, bullet in enumerate(bullets):
        if not isinstance(bullet, dict):
            raise ProfileError(f"{where}: bullet {index} should be a mapping")
        solo = bullet.get("solo")
        conn.execute(
            f"""
            INSERT INTO bullets ({parent_column}, {other}, text, metric, skills, solo, sort_order)
            VALUES (?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT ({parent_column}, sort_order) WHERE {parent_column} IS NOT NULL
            DO UPDATE SET
              text   = excluded.text,
              metric = COALESCE(excluded.metric, metric),
              skills = COALESCE(excluded.skills, skills),
              solo   = COALESCE(excluded.solo, solo)
            """,
            (
                parent_id,
                _required(bullet, "text", f"{where} bullet {index}"),
                _text(bullet, "metric"),
                _json_list(bullet, "skills"),
                None if solo is None else int(bool(solo)),
                index,
            ),
        )
    return len(bullets)


def _prune_bullets(
    conn: sqlite3.Connection, parent_column: str, parent_id: int, keep: int
) -> int:
    cursor = conn.execute(
        f"DELETE FROM bullets WHERE {parent_column} = ? AND sort_order >= ?",
        (parent_id, keep),
    )
    return cursor.rowcount


# =================================== load ===================================


def load(conn: sqlite3.Connection, *, prune: bool = False) -> dict[str, int]:
    """Import every profile file in one transaction. Returns row counts."""
    profile_dir = config.PROFILE_DIR
    # Parse everything before writing anything: a malformed second file must not
    # leave the first one half-imported.
    facts = _read_yaml(profile_dir / FACTS_FILE) or {}
    corpus = _read_yaml(profile_dir / EXPERIENCE_FILE) or {}
    if not isinstance(facts, dict):
        raise ProfileError(f"{FACTS_FILE}: expected a mapping at the top level")
    if not isinstance(corpus, dict):
        raise ProfileError(f"{EXPERIENCE_FILE}: expected a mapping at the top level")

    corpus_path = profile_dir / EXPERIENCE_FILE
    experiences = _expect_list(corpus.get("experiences"), corpus_path, "experiences")
    projects = _expect_list(corpus.get("projects"), corpus_path, "projects")
    education = _expect_list(corpus.get("education"), corpus_path, "education")
    credentials = _expect_list(corpus.get("credentials"), corpus_path, "credentials")
    languages = _expect_list(corpus.get("languages"), corpus_path, "languages")

    flat_facts = _flatten_facts(facts)
    counts = dict.fromkeys(
        ("facts", "experiences", "projects", "bullets", "education", "credentials",
         "languages", "pruned"),
        0,
    )
    now = config.utcnow()

    with transaction(conn):
        for key, value in flat_facts.items():
            conn.execute(
                """
                INSERT INTO profile_facts (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                                updated_at = excluded.updated_at
                WHERE value IS NOT excluded.value
                """,
                (key, value, now),
            )
        counts["facts"] = len(flat_facts)

        seen_experiences: list[int] = []
        for index, row in enumerate(experiences):
            where = f"experiences[{index}]"
            experience_id = _upsert(
                conn,
                "experiences",
                keys={
                    "company": _required(row, "company", where),
                    "title": _required(row, "title", where),
                    "start_month": _required(row, "start_month", where),
                },
                values={
                    "end_month": _text(row, "end_month"),
                    "location": _text(row, "location"),
                    "employment_type": _text(row, "employment_type"),
                    "company_context": _text(row, "company_context"),
                    "scope": _text(row, "scope"),
                    "leave_reason": _text(row, "leave_reason"),
                    "sort_order": index,
                    "titles_history": _json_list(row, "titles_history"),
                    "known_for": _text(row, "known_for"),
                    "recognition": _json_list(row, "recognition"),
                    "tech_built": _tech(row, "built"),
                    "tech_owned": _tech(row, "owned"),
                    "tech_maintained": _tech(row, "maintained"),
                    "tech_touched": _tech(row, "touched"),
                },
                conflict="(company, title, start_month)",
            )
            seen_experiences.append(experience_id)
            bullets = row.get("bullets") or []
            counts["bullets"] += _load_bullets(
                conn, "experience_id", experience_id, bullets, where
            )
            if prune:
                counts["pruned"] += _prune_bullets(
                    conn, "experience_id", experience_id, len(bullets)
                )
        counts["experiences"] = len(seen_experiences)

        seen_projects: list[int] = []
        for index, row in enumerate(projects):
            where = f"projects[{index}]"
            project_id = _upsert(
                conn,
                "projects",
                keys={"name": _required(row, "name", where)},
                values={
                    "url": _text(row, "url"),
                    "blurb": _text(row, "blurb"),
                    "sort_order": index,
                    "role": _text(row, "role"),
                    "traction": _text(row, "traction"),
                    "scope": _text(row, "scope"),
                    "start_month": _text(row, "start_month"),
                    "end_month": _text(row, "end_month"),
                    "tech_built": _tech(row, "built"),
                    "tech_owned": _tech(row, "owned"),
                    "tech_maintained": _tech(row, "maintained"),
                    "tech_touched": _tech(row, "touched"),
                },
                conflict="(name)",
            )
            seen_projects.append(project_id)
            bullets = row.get("bullets") or []
            counts["bullets"] += _load_bullets(
                conn, "project_id", project_id, bullets, where
            )
            if prune:
                counts["pruned"] += _prune_bullets(
                    conn, "project_id", project_id, len(bullets)
                )
        counts["projects"] = len(seen_projects)

        seen_education: list[int] = []
        for index, row in enumerate(education):
            where = f"education[{index}]"
            complete = row.get("complete")
            seen_education.append(
                _upsert(
                    conn,
                    "education",
                    keys={
                        "school": _required(row, "school", where),
                        "degree": _text(row, "degree"),
                        "field": _text(row, "field"),
                    },
                    values={
                        "start_month": _text(row, "start_month"),
                        "end_month": _text(row, "end_month"),
                        "complete": 1 if complete is None else int(bool(complete)),
                        "notes": _text(row, "notes"),
                    },
                    # COALESCE matches idx_education_natural (004). `degree` and
                    # `field` are nullable, and a plain (school, degree, field)
                    # target never fires for a NULL key — the row duplicates on
                    # every run instead of upserting.
                    conflict="(school, COALESCE(degree, ''), COALESCE(field, ''))",
                )
            )
        counts["education"] = len(seen_education)

        seen_credentials: list[int] = []
        for index, row in enumerate(credentials):
            where = f"credentials[{index}]"
            seen_credentials.append(
                _upsert(
                    conn,
                    "credentials",
                    keys={
                        "kind": _required(row, "kind", where),
                        "title": _required(row, "title", where),
                        "issuer": _text(row, "issuer"),
                    },
                    values={
                        "issued": _text(row, "issued"),
                        "expires": _text(row, "expires"),
                        "url": _text(row, "url"),
                    },
                    # COALESCE matches idx_credentials_natural (004); `issuer` is
                    # nullable. See the education upsert above.
                    conflict="(kind, title, COALESCE(issuer, ''))",
                )
            )
        counts["credentials"] = len(seen_credentials)

        seen_languages: list[int] = []
        for index, row in enumerate(languages):
            where = f"languages[{index}]"
            seen_languages.append(
                _upsert(
                    conn,
                    "languages",
                    keys={"language": _required(row, "language", where)},
                    values={"fluency": _text(row, "fluency")},
                    conflict="(language)",
                )
            )
        counts["languages"] = len(seen_languages)

        if prune:
            counts["pruned"] += _prune_missing(
                conn, "experiences", seen_experiences, child_column="experience_id"
            )
            counts["pruned"] += _prune_missing(
                conn, "projects", seen_projects, child_column="project_id"
            )
            counts["pruned"] += _prune_missing(conn, "education", seen_education)
            counts["pruned"] += _prune_missing(conn, "credentials", seen_credentials)
            counts["pruned"] += _prune_missing(conn, "languages", seen_languages)

    return counts


def _prune_missing(
    conn: sqlite3.Connection,
    table: str,
    keep: list[int],
    *,
    child_column: str | None = None,
) -> int:
    """Delete rows the files no longer mention. Only ever called under --prune.

    `bullets` rows point at experiences and projects, so a parent cannot be
    deleted while its bullets are still there — foreign keys are enforced on
    every connection. Clear the children first, in the same transaction.
    """
    removed = 0
    if child_column is not None:
        if keep:
            marks = ", ".join("?" for _ in keep)
            cursor = conn.execute(
                f"DELETE FROM bullets WHERE {child_column} IS NOT NULL "
                f"AND {child_column} NOT IN ({marks})",
                tuple(keep),
            )
        else:
            cursor = conn.execute(
                f"DELETE FROM bullets WHERE {child_column} IS NOT NULL"
            )
        removed += cursor.rowcount

    if keep:
        marks = ", ".join("?" for _ in keep)
        cursor = conn.execute(f"DELETE FROM {table} WHERE id NOT IN ({marks})", tuple(keep))
    else:
        cursor = conn.execute(f"DELETE FROM {table}")
    return removed + cursor.rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import docs/profile/* into SQLite.")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete rows the profile files no longer mention (off by default)",
    )
    args = parser.parse_args(argv)

    conn = connect()
    try:
        counts = load(conn, prune=args.prune)
    except ProfileError as exc:
        print(f"profile not loaded: {exc}", file=sys.stderr)
        return 1
    except sqlite3.IntegrityError as exc:
        # Most likely under --prune: something outside the profile files, a
        # `stories` row for instance, still points at a row being removed.
        print(
            f"profile not loaded, nothing was written: {exc}\n"
            "Under --prune this usually means another table still references a "
            "row the files no longer mention.",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()

    # Counts only. Employer names stay out of log output.
    order = ("facts", "experiences", "projects", "bullets", "education", "credentials",
             "languages")
    print(" · ".join(f"{counts[name]} {name}" for name in order))
    if args.prune:
        print(f"pruned {counts['pruned']} row(s) no longer in the files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
