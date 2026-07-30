"""sqlite3 connection management.

One writer host — the Mac mini's process is the only thing that opens the file.
Laptops go through HTTP over Tailscale. See docs/architecture.md, Deployment.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import config

# WAL plus file-level sync corrupts the database. Refuse rather than discover it later.
SYNC_MARKERS = (
    "Library/Mobile Documents",  # iCloud Drive
    "Dropbox",
    "Google Drive",
    "OneDrive",
    "iCloud",
)


class SyncedDatabaseError(RuntimeError):
    """The DB path lives in a file-sync folder."""


def assert_not_synced(path: Path) -> None:
    text = str(path)
    for marker in SYNC_MARKERS:
        if marker in text:
            raise SyncedDatabaseError(
                f"refusing to open {path}: {marker!r} is a sync folder, and WAL mode "
                "plus file-level sync corrupts SQLite. Keep the DB on local disk."
            )


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas this codebase assumes.

    `foreign_keys` defaults OFF per connection — SQLite silently ignores FK
    violations otherwise, so it is set here on every connection, not once at
    creation.
    """
    db_path = Path(path) if path is not None else config.DB_PATH
    assert_not_synced(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None: no implicit BEGIN. Transactions are explicit, below.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. Nests by joining the caller's transaction.

    A state change and its `events` row must commit together; nesting has to
    join rather than open a second transaction, or the guarantee breaks.
    """
    if conn.in_transaction:
        yield conn  # outer caller owns the commit
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def verify_schema(conn: sqlite3.Connection) -> None:
    """Startup check: WAL is on and migrations have run."""
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != "wal":
        raise RuntimeError(f"expected WAL journal mode, got {mode!r}")
    missing = [
        table
        for table in ("applications", "events", "jobs", "companies")
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    ]
    if missing:
        raise RuntimeError(f"schema incomplete, missing {missing}. Run `make migrate`.")
