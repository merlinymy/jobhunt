"""Nightly snapshot of the database. `python -m jobhunt.backup`.

The DB holds the exact bytes of every resume actually submitted, which cannot be
regenerated from current templates — the whole point of storing them. It lives on
an external disk that can fail or be unplugged. So: a consistent copy every night
onto the *internal* disk, verified by opening it, pruned on a schedule that never
throws away the only good copy, and pulled to a laptop periodically.

Python rather than a shell script because a script would have to re-derive
`DB_PATH`, re-implement the volume guard, and hard-code the table list — which is
exactly the class of duplication that let `install.sh` bake a divergent path into
a plist. Here `db.connect()` runs the same guard as everything else.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db

STAMP = "%Y%m%dT%H%M%SZ"
PREFIX = "jobhunt-"
SUFFIX = ".db"

# Counted in every verify. A backup where one of these went backwards is a bug
# worth stopping on, not a file to quietly rotate into the keep set.
TABLES = ("companies", "jobs", "applications", "events", "llm_calls")

KEEP_DAILY = 14
KEEP_WEEKLY = 8
KEEP_MONTHLY = 12


class BackupError(RuntimeError):
    """A snapshot could not be taken, or could not be trusted."""


def _stamp_of(path: Path) -> datetime | None:
    name = path.name
    if not name.startswith(PREFIX) or not name.endswith(SUFFIX):
        return None
    try:
        return datetime.strptime(name[len(PREFIX) : -len(SUFFIX)], STAMP).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def snapshots(dest_dir: Path) -> list[Path]:
    """Every complete snapshot, oldest first. `.partial` and `.FAILED` excluded."""
    found = [(s, p) for p in dest_dir.glob(f"{PREFIX}*{SUFFIX}") if (s := _stamp_of(p))]
    return [p for _, p in sorted(found)]


def snapshot(dest_dir: Path | None = None) -> Path:
    """One consistent copy of the live database.

    `VACUUM INTO` rather than `Connection.backup()`: a single statement, taken
    under a read transaction so writers are never blocked, defragmented on the
    way out, and the result is a standalone file with no `-wal` sidecar to keep
    together. It refuses an existing target, hence the unlink.

    Written to `.partial` and renamed, so the laptop's rsync can never catch a
    half-written file — the final name only ever exists on a complete one.
    """
    dest_dir = dest_dir or config.BACKUP_DIR
    db.assert_not_synced(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    final = dest_dir / f"{PREFIX}{datetime.now(timezone.utc).strftime(STAMP)}{SUFFIX}"
    partial = final.with_name(final.name + ".partial")
    partial.unlink(missing_ok=True)

    conn = db.connect()  # runs the volume guard
    try:
        conn.execute("VACUUM INTO ?", (str(partial),))
    except sqlite3.Error as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"VACUUM INTO failed: {exc}") from exc
    finally:
        conn.close()

    os.replace(partial, final)
    final.chmod(0o600)
    return final


def verify(path: Path, *, expect: dict[str, int] | None = None) -> dict[str, int]:
    """Open the snapshot and prove it is whole. A backup you have not read is a rumour.

    Full `integrity_check`, not `quick_check`: it reads every page, which for this
    database means every stored PDF. That is the cheapest strong evidence the file
    is intact, and the blobs are the part that cannot be regenerated.
    """
    if not path.is_file():
        raise BackupError(f"{path} does not exist")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"{path.name}: integrity_check says {result!r}")
        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        if dangling:
            raise BackupError(f"{path.name}: {len(dangling)} dangling foreign keys")
        applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        on_disk = len(list(config.MIGRATIONS_DIR.glob("*.sql")))
        if applied != on_disk:
            raise BackupError(
                f"{path.name}: {applied} migrations applied but {on_disk} on disk"
            )
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
    finally:
        conn.close()
    if expect:
        shrunk = {t: (counts[t], expect[t]) for t in TABLES if counts[t] < expect[t]}
        if shrunk:
            raise BackupError(f"{path.name}: rows went backwards: {shrunk}")
    return counts


def keepers(paths: list[Path]) -> set[Path]:
    """Grandfather-father-son. Newest per day, then per ISO week, then per month."""
    dated = sorted(((s, p) for p in paths if (s := _stamp_of(p))), reverse=True)
    if not dated:
        return set()
    keep = {dated[0][1]}  # the newest is never a candidate for pruning
    now = datetime.now(timezone.utc)
    for window, span, key in (
        (KEEP_DAILY, timedelta(days=KEEP_DAILY), lambda d: d.date()),
        (KEEP_WEEKLY, timedelta(weeks=KEEP_WEEKLY), lambda d: d.isocalendar()[:2]),
        (KEEP_MONTHLY, timedelta(days=31 * KEEP_MONTHLY), lambda d: (d.year, d.month)),
    ):
        seen: set = set()
        for stamp, path in dated:  # newest first, so the first per bucket wins
            if now - stamp > span:
                break
            bucket = key(stamp)
            if bucket not in seen and len(seen) < window:
                seen.add(bucket)
                keep.add(path)
    return keep


def prune(dest_dir: Path | None = None, *, dry_run: bool = False) -> list[Path]:
    dest_dir = dest_dir or config.BACKUP_DIR
    existing = snapshots(dest_dir)
    keep = keepers(existing)
    removed = [p for p in existing if p not in keep]
    if not dry_run:
        for path in removed:
            path.unlink()
    return removed


def save_env(dest_dir: Path | None = None) -> Path | None:
    """A rolling 0600 copy of `.env` beside the snapshots.

    A restore without the API key and `JOBHUNT_DB_VOLUME_ID` is not a restore —
    you get a database you cannot score against and a volume guard that rejects
    the disk. Same secrets as the DB, same directory, same permissions.
    """
    source = config.REPO_ROOT / ".env"
    if not source.is_file():
        return None
    dest_dir = dest_dir or config.BACKUP_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / "env-latest"
    target.write_bytes(source.read_bytes())
    target.chmod(0o600)
    return target


def drill(dest_dir: Path | None = None) -> dict[str, int]:
    """Prove the app can actually run on the newest snapshot, not just read it.

    Copies it aside, migrates against the copy (which must report no pending
    work), then runs the startup schema check and one real query. This is the
    difference between "a file exists" and "a restore would work".
    """
    dest_dir = dest_dir or config.BACKUP_DIR
    existing = snapshots(dest_dir)
    if not existing:
        raise BackupError(f"nothing to drill in {dest_dir}")
    newest = existing[-1]
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "drill.db"
        shutil.copy2(newest, copy)
        copy.chmod(0o600)
        env = {**os.environ, "JOBHUNT_DB": str(copy)}
        env.pop("JOBHUNT_DB_VOLUME_ID", None)  # the copy is on the boot volume
        proc = subprocess.run(
            [sys.executable, "-m", "jobhunt.migrate"],
            capture_output=True, text=True, env=env, cwd=config.REPO_ROOT,
        )
        if proc.returncode != 0:
            raise BackupError(f"drill: migrate failed on {newest.name}\n{proc.stderr}")
        if "creating a new database" in proc.stderr:
            raise BackupError(f"drill: migrate did not find {copy} — it created one")
        if "up to date" not in proc.stdout:
            raise BackupError(
                f"drill: {newest.name} needed migrations, so it predates this "
                f"checkout's schema:\n{proc.stdout.strip()}"
            )
        conn = db.connect(copy)
        try:
            db.verify_schema(conn)
            counts = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in TABLES
            }
        finally:
            conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="snapshot and verify the database")
    parser.add_argument("--verify", metavar="PATH", help="verify one snapshot and exit")
    parser.add_argument("--drill", action="store_true", help="restore-test the newest")
    parser.add_argument("--no-prune", action="store_true", help="keep every snapshot")
    parser.add_argument("--no-env", action="store_true", help="do not copy .env")
    parser.add_argument("--dir", metavar="PATH", help="override the backup directory")
    args = parser.parse_args(argv)

    dest = Path(args.dir).expanduser() if args.dir else config.BACKUP_DIR

    if args.verify:
        try:
            counts = verify(Path(args.verify).expanduser())
        except BackupError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
        print(f"ok   {args.verify}  {counts}")
        return 0

    if args.drill:
        try:
            counts = drill(dest)
        except BackupError as exc:
            print(f"FAIL drill: {exc}", file=sys.stderr)
            return 1
        print(f"ok   drill  the app runs on the newest snapshot  {counts}")
        return 0

    try:
        live = _live_counts()
        path = snapshot(dest)
    except (BackupError, db.DatabaseLocationError) as exc:
        print(f"FAIL snapshot: {exc}", file=sys.stderr)
        return 1

    try:
        counts = verify(path, expect=live)
    except BackupError as exc:
        # Keep the evidence, and do not prune: never delete history on the
        # strength of a backup that could not be read.
        broken = path.with_suffix(path.suffix + ".FAILED")
        path.rename(broken)
        print(f"FAIL {exc}\n     kept as {broken.name}; nothing pruned", file=sys.stderr)
        return 1

    size = path.stat().st_size
    print(f"ok   {path.name}  {size / 1024 / 1024:.1f}MB  {counts}")

    if not args.no_env and (saved := save_env(dest)):
        print(f"ok   {saved.name}  (0600)")

    if not args.no_prune:
        removed = prune(dest)
        kept = len(snapshots(dest))
        print(f"ok   pruned {len(removed)}, kept {kept}")
    return 0


def _live_counts() -> dict[str, int]:
    conn = db.connect()
    try:
        return {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
