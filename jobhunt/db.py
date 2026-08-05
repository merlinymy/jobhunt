"""sqlite3 connection management.

One writer host — the mini's process is the only thing that opens the file.
Laptops go through HTTP over Tailscale Serve. See docs/architecture.md, Deployment.

The database lives on an external SSD, which introduces a failure this module
exists to make impossible: a path under `/Volumes` that is not mounted is still
a perfectly writable path on the boot volume. See `assert_volume_ready`.
"""

from __future__ import annotations

import os
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

# Written to the DB's own directory by `install.sh --init-volume`. Holds a UUID
# matched against JOBHUNT_DB_VOLUME_ID, so a *different* disk mounted at the
# right path is caught too.
SENTINEL_NAME = ".jobhunt-volume"

_SYNCHRONOUS = ("OFF", "NORMAL", "FULL", "EXTRA")


class DatabaseLocationError(RuntimeError):
    """The DB path is not somewhere we are willing to open a database."""


class SyncedDatabaseError(DatabaseLocationError):
    """The DB path lives in a file-sync folder."""


class VolumeNotReadyError(DatabaseLocationError):
    """The external volume is missing, or the wrong disk is mounted."""


class DatabaseMissingError(DatabaseLocationError):
    """The volume is fine but the database file is not there."""


def assert_not_synced(path: Path) -> None:
    text = str(path)
    for marker in SYNC_MARKERS:
        if marker in text:
            raise SyncedDatabaseError(
                f"refusing to open {path}: {marker!r} is a sync folder, and WAL mode "
                "plus file-level sync corrupts SQLite. Keep the DB on local disk."
            )


def resolve(path: Path | str | None = None) -> Path:
    """The one place a DB path becomes absolute.

    `config.DB_PATH` applies `.expanduser()` but not `.resolve()`, so a relative
    `JOBHUNT_DB` used to resolve against the process CWD — which differs between
    a shell, a launchd agent, and pytest. `abspath` rather than `Path.resolve()`
    so a symlinked volume keeps the name the mount check needs to see.
    """
    raw = Path(path) if path is not None else config.DB_PATH
    return Path(os.path.abspath(raw.expanduser()))


def volume_root(path: Path) -> Path | None:
    """`/Volumes/<name>` when `path` is on an external volume, else None.

    Returning None for everything else is what keeps the guard off the boot
    volume: the repo-local dev DB and pytest's tmp_path need no mount and no
    sentinel, so they are unaffected by any of this.
    """
    parts = path.parts
    if len(parts) >= 3 and parts[0] == os.sep and parts[1] == "Volumes":
        return Path(os.sep, "Volumes", parts[2])
    return None


def assert_volume_ready(db_path: Path) -> None:
    """Refuse to touch a `/Volumes` path unless the right disk is mounted.

    Without this, opening the DB with the SSD unplugged creates `/Volumes/<name>/`
    as an ordinary directory on the boot volume and puts an empty database inside
    it. Every write until someone notices lands in that decoy; when the SSD is
    plugged back in macOS mounts it alongside as `/Volumes/<name> 1`, the real
    database reappears, and the writes in between are orphaned where nothing will
    ever look for them.

    Three checks because each catches a different failure: `ismount` catches the
    decoy directory, the sentinel catches a different disk mounted at the right
    path, and `connect`'s `mode=rw` catches whatever is left, including the race
    between this check and the open.
    """
    root = volume_root(db_path)
    if root is None:
        return
    if not os.path.ismount(root):
        detail = ""
        if root.is_dir():
            detail = (
                f"\n  {root} already exists as a plain directory. Remove it — otherwise "
                f"macOS mounts the real disk beside it as '{root} 1':\n"
                f"    rmdir '{root}'"
            )
        raise VolumeNotReadyError(
            f"{root} is not a mount point, so the external disk is not mounted. "
            f"Refusing to touch {db_path}: creating it here would put a decoy "
            f"database on the boot volume." + detail
        )
    sentinel = db_path.parent / SENTINEL_NAME
    try:
        found = sentinel.read_text().split("\n", 1)[0].strip()
    except OSError:
        raise VolumeNotReadyError(
            f"no {SENTINEL_NAME} beside {db_path}: the disk mounted at {root} is not "
            f"the jobhunt volume, or it was never initialised. Run "
            f"`./deploy/install.sh --init-volume`."
        ) from None
    want = (os.environ.get("JOBHUNT_DB_VOLUME_ID") or "").strip()
    if want and found != want:
        raise VolumeNotReadyError(
            f"wrong disk mounted at {root}: {SENTINEL_NAME} reads {found!r} but "
            f"JOBHUNT_DB_VOLUME_ID is {want!r}."
        )


def connect(
    path: Path | str | None = None, *, create: bool | None = None
) -> sqlite3.Connection:
    """Open a connection with the pragmas this codebase assumes.

    `create` is off by default, which is the whole point: `sqlite3.connect` will
    happily manufacture an empty database at any writable path, and every way
    that helps is outweighed by the one way it hurts. `migrate.py` passes
    `create=True`; nothing else should. `JOBHUNT_DB_CREATE=1` is the escape hatch
    for first-time setup, and belongs in a shell, never in a plist.

    `foreign_keys` defaults OFF per connection — SQLite silently ignores FK
    violations otherwise, so it is set here on every connection, not once at
    creation.
    """
    db_path = resolve(path)
    assert_not_synced(db_path)
    assert_volume_ready(db_path)

    if create is None:
        create = os.environ.get("JOBHUNT_DB_CREATE") == "1"
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "rwc"
    else:
        if not db_path.is_file():
            raise DatabaseMissingError(
                f"no database at {db_path}, and nothing outside `make migrate` creates "
                f"one. If this is first-time setup, run `make migrate`. If it is not, "
                f"the volume is mounted but the file is gone — check a backup before "
                f"doing anything else."
            )
        # SQLite will not create through a URI without the `c`. This is the check
        # that survives the volume being pulled between the guard above and here.
        mode = "rw"

    # isolation_level=None: no implicit BEGIN. Transactions are explicit, below.
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode={mode}", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # NORMAL can lose the last transactions on power loss. An external disk can
    # also simply be unplugged, so the mini sets FULL in .env; the default stays
    # NORMAL so dev and the test fixtures are not paying an fsync per commit.
    sync = (os.environ.get("JOBHUNT_SQLITE_SYNCHRONOUS") or "NORMAL").upper()
    if sync not in _SYNCHRONOUS:
        raise ValueError(
            f"JOBHUNT_SQLITE_SYNCHRONOUS={sync!r} is not one of {list(_SYNCHRONOUS)}"
        )
    conn.execute(f"PRAGMA synchronous = {sync}")
    # 5s was tight enough that a 06:30 discovery run overlapping a dashboard click
    # could 500. Every transaction here is short; waiting is always better than failing.
    conn.execute("PRAGMA busy_timeout = 15000")
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
