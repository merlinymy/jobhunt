"""Apply migrations/*.sql in order, once each.

`schema_migrations` is bookkeeping, not domain schema, so it is created here
rather than in 001_init.sql — which must never be edited after it has run.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

from . import config
from .db import connect, transaction

BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  sha256     TEXT NOT NULL,
  applied_at TEXT NOT NULL
)
"""


class MigrationChanged(RuntimeError):
    """An already-applied migration file was edited."""


def pending(conn: sqlite3.Connection) -> list[Path]:
    conn.execute(BOOKKEEPING)
    applied = {
        row["filename"]: row["sha256"]
        for row in conn.execute("SELECT filename, sha256 FROM schema_migrations")
    }
    todo = []
    for path in sorted(config.MIGRATIONS_DIR.glob("*.sql")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.name not in applied:
            todo.append(path)
        elif applied[path.name] != digest:
            raise MigrationChanged(
                f"{path.name} has changed since it was applied. Migrations are "
                "immutable once run — add the next numbered file instead."
            )
    return todo


def apply(conn: sqlite3.Connection, path: Path) -> None:
    sql = path.read_text()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # executescript() commits any open transaction, so PRAGMAs inside the file
    # (journal_mode) work, but the bookkeeping insert has to follow separately.
    conn.executescript(sql)
    with transaction(conn):
        conn.execute(
            "INSERT INTO schema_migrations (filename, sha256, applied_at) VALUES (?, ?, ?)",
            (path.name, digest, config.utcnow()),
        )


def main() -> int:
    conn = connect()
    todo = pending(conn)
    if not todo:
        print(f"up to date — {config.DB_PATH}")
        return 0
    for path in todo:
        apply(conn, path)
        print(f"applied {path.name}")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"{config.DB_PATH} — journal_mode={mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
