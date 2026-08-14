"""Apply migrations/*.sql in order, once each.

`schema_migrations` is bookkeeping, not domain schema, so it is created here
rather than in 001_init.sql — which must never be edited after it has run.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

from . import config
from .db import connect

BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  sha256     TEXT NOT NULL,
  applied_at TEXT NOT NULL
)
"""


class MigrationChanged(RuntimeError):
    """An already-applied migration file was edited."""


class MigrationNumberClash(RuntimeError):
    """Two migration files claim the same number, and one has not run yet."""


_NUMBER = re.compile(r"^(\d+)_")

# The one number ever issued twice. Both files shipped long before this check
# existed, and a migration cannot be renamed once it has run.
#
# Named explicitly rather than inferred from what the database has already
# applied, which is the version of this that got written first and was wrong in
# the one case that matters: on a *fresh* database nothing is applied, so both of
# these are pending, the clash fires, and `make migrate` can never create a
# database at all. That path is first-time setup, `make test`, and the weekly
# restore drill — the three places a bootstrap failure is worst. A rule about
# filenames should not consult the database, and now it does not.
GRANDFATHERED = frozenset({"010_resume_findings.sql", "010_runs.sql"})


def check_numbering(paths: list[Path]) -> None:
    """Refuse a migration whose number another file already claims.

    Files are applied in `sorted()` order, which is by filename — so two files
    numbered 010 run in alphabetical order of whatever follows the number, which
    is not an order anybody chose. The pair above is exactly that: they are
    independent, so the order between them happens not to matter, and the next
    collision will not be so lucky. This is what stops a third file joining them,
    or a new one reusing any number already spent.
    """
    numbered: dict[str, list[Path]] = {}
    for path in paths:
        match = _NUMBER.match(path.name)
        if match:
            numbered.setdefault(match.group(1).lstrip("0") or "0", []).append(path)
    for number, group in sorted(numbered.items()):
        if len(group) < 2:
            continue
        offenders = sorted(p.name for p in group if p.name not in GRANDFATHERED)
        if offenders:
            raise MigrationNumberClash(
                f"migration number {number} is claimed by {len(group)} files — "
                f"{sorted(p.name for p in group)}. They apply in filename order, so "
                f"the order between them is alphabetical rather than chosen. "
                f"Renumber {offenders} to the next free number."
            )


def pending(conn: sqlite3.Connection) -> list[Path]:
    conn.execute(BOOKKEEPING)
    applied = {
        row["filename"]: row["sha256"]
        for row in conn.execute("SELECT filename, sha256 FROM schema_migrations")
    }
    on_disk = sorted(config.MIGRATIONS_DIR.glob("*.sql"))
    check_numbering(on_disk)
    todo = []
    for path in on_disk:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.name not in applied:
            todo.append(path)
        elif applied[path.name] != digest:
            raise MigrationChanged(
                f"{path.name} has changed since it was applied. Migrations are "
                "immutable once run — add the next numbered file instead."
            )
    return todo


class ForeignKeysBroken(RuntimeError):
    """A migration left dangling references behind."""


def apply(conn: sqlite3.Connection, path: Path) -> None:
    """Apply one file, all of it or none of it.

    `executescript()` commits any open transaction before it runs, so the BEGIN
    has to live inside the script text — wrapping the call in `transaction()`
    would be silently discarded. Without this, a file whose third statement is a
    syntax error leaves statements one and two committed and unrecorded, and
    every later `make migrate` fails on "table already exists".

    The script deliberately has no COMMIT: the transaction stays open so the
    bookkeeping row can be inserted with real parameters rather than string
    interpolation, and both commit together.
    """
    sql = path.read_text()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        conn.executescript(f"BEGIN;\n{sql}")
        # Foreign keys are off for the run (see main), which is what makes the
        # rebuild-and-rename dance legal. Verify before committing, not never.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ForeignKeysBroken(
                f"{path.name} left {len(violations)} dangling reference(s): "
                f"{[tuple(row) for row in violations[:5]]}"
            )
        conn.execute(
            "INSERT INTO schema_migrations (filename, sha256, applied_at) VALUES (?, ?, ?)",
            (path.name, digest, config.utcnow()),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def main() -> int:
    # The only caller that may create a database. Everything else opens `mode=rw`
    # and raises, so a mistyped path or an unmounted disk can never quietly
    # become an empty second database that then diverges from the real one.
    fresh = not config.DB_PATH.expanduser().is_file()
    conn = connect(create=True)
    if fresh:
        # Loud, because the common cause is a wrong JOBHUNT_DB rather than an
        # actual first run, and the symptom otherwise is "where did my data go".
        print(f"creating a new database at {config.DB_PATH}", file=sys.stderr)
    todo = pending(conn)
    if not todo:
        print(f"up to date — {config.DB_PATH}")
        return 0
    # Off for the duration, restored below. SQLite cannot add a constraint in
    # place: the documented way is to rebuild the table and rename it over the
    # old one, and that is only safe with FK enforcement suspended. Each file's
    # transaction runs `foreign_key_check` before it commits, so nothing slips.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for path in todo:
            apply(conn, path)
            print(f"applied {path.name}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"{config.DB_PATH} — journal_mode={mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
