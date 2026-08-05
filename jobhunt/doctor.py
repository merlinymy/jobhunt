"""Is this deployment healthy? `python -m jobhunt.doctor`.

One place that knows what "ready" means, so the launchd wrappers can ask instead
of re-deriving mount rules in bash — which is how `install.sh` ended up reading
`JOBHUNT_DB` from the installing shell and baking a divergent path into a plist.

Every check is independent and none of them raise: the point of a health report
is to show you all six problems at once, not to stop at the first.

    python -m jobhunt.doctor                     full report, exit 1 on any failure
    python -m jobhunt.doctor --require-db        just the availability gate
    python -m jobhunt.doctor --require-db --wait 120 --quiet
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

# A backup older than this means the 03:30 agent has missed a night.
STALE_BACKUP_HOURS = 36
BIG_LOG_BYTES = 50 * 1024 * 1024
LOW_DISK_BYTES = 2 * 1024 * 1024 * 1024


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False  # true = report it, but do not fail the run


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def db_available() -> Check:
    """The gate the launchd wrappers call. Mount, sentinel, and file present."""
    try:
        path = db.resolve()
    except Exception as exc:
        return Check("database", False, f"cannot resolve the path: {exc}")
    try:
        db.assert_not_synced(path)
        db.assert_volume_ready(path)
    except db.DatabaseLocationError as exc:
        return Check("database", False, str(exc).split("\n")[0])
    if not path.is_file():
        return Check("database", False, f"{path} does not exist — run `make migrate`")
    root = db.volume_root(path)
    where = f"on {root}" if root else "on the boot volume"
    return Check("database", True, f"{path} ({_human(path.stat().st_size)}, {where})")


def db_integrity() -> Check:
    try:
        conn = db.connect()
    except Exception as exc:
        return Check("integrity", False, f"cannot open: {type(exc).__name__}")
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        return Check("integrity", False, f"quick_check says {result!r}")
    if mode.lower() != "wal":
        return Check("integrity", False, f"journal_mode is {mode!r}, expected wal")
    return Check("integrity", True, f"quick_check ok, wal, synchronous={sync}")


def migrations() -> Check:
    on_disk = sorted(p.name for p in config.MIGRATIONS_DIR.glob("*.sql"))
    try:
        conn = db.connect()
    except Exception as exc:
        return Check("migrations", False, f"cannot open: {type(exc).__name__}")
    try:
        applied = {
            row[0] for row in conn.execute("SELECT filename FROM schema_migrations")
        }
    except sqlite3.OperationalError:
        return Check("migrations", False, "no schema_migrations — run `make migrate`")
    finally:
        conn.close()
    missing = [name for name in on_disk if name not in applied]
    extra = sorted(applied - set(on_disk))
    if missing:
        return Check("migrations", False, f"{len(missing)} pending: {missing[:3]}")
    if extra:
        # The DB has been migrated by a newer checkout than this one. Deploying
        # this code over it would run against a schema it does not know about.
        return Check("migrations", False, f"applied but not in this checkout: {extra}")
    return Check("migrations", True, f"{len(on_disk)} applied, none pending")


def disk_space() -> Check:
    parts = []
    worst_ok = True
    seen: set[str] = set()
    for label, path in (("db", db.resolve().parent), ("backups", config.BACKUP_DIR)):
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except OSError as exc:
            parts.append(f"{label}: {exc.strerror}")
            worst_ok = False
            continue
        key = f"{usage.total}:{usage.free}"
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{label} {_human(usage.free)} free")
        if usage.free < LOW_DISK_BYTES:
            worst_ok = False
    return Check("disk", worst_ok, ", ".join(parts))


def backups() -> Check:
    if not config.BACKUP_DIR.is_dir():
        return Check("backups", False, f"{config.BACKUP_DIR} does not exist")
    failed = sorted(config.BACKUP_DIR.glob("*.FAILED"))
    snapshots = sorted(config.BACKUP_DIR.glob("jobhunt-*.db"))
    if not snapshots:
        return Check("backups", False, f"no snapshots in {config.BACKUP_DIR}")
    newest = max(snapshots, key=lambda p: p.stat().st_mtime)
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    detail = f"{len(snapshots)} snapshots, newest {age_h:.1f}h old ({_human(newest.stat().st_size)})"
    if failed:
        return Check("backups", False, f"{detail}; {len(failed)} FAILED left in place")
    if age_h > STALE_BACKUP_HOURS:
        return Check("backups", False, f"{detail} — the nightly agent has missed a run")
    return Check("backups", True, detail)


def extras() -> Check:
    """The optional deps are imported lazily inside functions, so a venv missing
    them installs clean and then dies at 06:30 on `import jobspy`."""
    wanted = {"anthropic": "llm", "jobspy": "ingest", "rendercv": "resume"}
    missing = [m for m in wanted if importlib.util.find_spec(m) is None]
    if missing:
        groups = ",".join(sorted({wanted[m] for m in missing}))
        return Check(
            "extras", False, f"missing {missing} — uv pip install -e '.[{groups}]'"
        )
    return Check("extras", True, f"{', '.join(sorted(wanted))} importable")


def secrets() -> Check:
    """Presence only. The value never goes anywhere near a log."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return Check("secrets", False, "ANTHROPIC_API_KEY is not set — see .env")
    return Check("secrets", True, "ANTHROPIC_API_KEY set")


def bind() -> Check:
    import ipaddress

    try:
        address = ipaddress.ip_address(config.HOST)
        loopback = address.is_loopback
    except ValueError:
        loopback = config.HOST in ("localhost", "localhost.")
    if not loopback:
        return Check(
            "bind", False, f"JOBHUNT_HOST={config.HOST} is not loopback; serve() refuses"
        )
    return Check("bind", True, f"{config.HOST}:{config.PORT}, loopback")


def tailscale() -> Check:
    """Advisory. A laptop with no tailscale installed is not broken."""
    binary = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not Path(binary).exists():
        return Check("tailscale", True, "not installed (local-only access)", warn=True)
    try:
        proc = subprocess.run(
            [binary, "serve", "status"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("tailscale", True, f"could not query: {exc}", warn=True)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0 or not output or "No serve config" in output:
        return Check(
            "tailscale",
            True,
            "installed but nothing served — "
            f"tailscale serve --bg --https=443 http://127.0.0.1:{config.PORT}",
            warn=True,
        )
    return Check("tailscale", True, output.splitlines()[0].strip())


def logs() -> Check:
    if not config.LOG_DIR.is_dir():
        return Check("logs", True, f"{config.LOG_DIR} not created yet", warn=True)
    big = [
        f"{p.name} {_human(p.stat().st_size)}"
        for p in sorted(config.LOG_DIR.glob("*.log"))
        if p.stat().st_size > BIG_LOG_BYTES
    ]
    if big:
        return Check("logs", False, f"oversized: {', '.join(big)}")
    count = len(list(config.LOG_DIR.glob("*.log")))
    return Check("logs", True, f"{count} files in {config.LOG_DIR}")


CHECKS = (
    db_available,
    db_integrity,
    migrations,
    disk_space,
    backups,
    extras,
    secrets,
    bind,
    tailscale,
    logs,
)


def run_checks() -> list[Check]:
    results: list[Check] = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # a broken check must not hide the other nine
            results.append(Check(check.__name__, False, f"check raised: {exc!r}"))
    return results


def wait_for_db(timeout: float = 0.0, interval: float = 5.0) -> None:
    """Block until the database is openable, or raise.

    The dashboard runs under `KeepAlive`, so without this an unmounted disk at
    login means a traceback every 30 seconds forever. Waiting inside the process
    turns that into one quiet sleeper that recovers the moment the disk appears.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        check = db_available()
        if check.ok:
            return
        if time.monotonic() >= deadline:
            raise db.DatabaseLocationError(check.detail)
        print(
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"waiting for the database: {check.detail}",
            flush=True,
        )
        time.sleep(min(interval, max(deadline - time.monotonic(), 0.1)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check the jobhunt deployment")
    parser.add_argument(
        "--require-db",
        action="store_true",
        help="only check that the database is available",
    )
    parser.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="with --require-db, wait this long for the volume to appear",
    )
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args(argv)

    if args.require_db:
        if args.wait:
            try:
                wait_for_db(args.wait)
            except db.DatabaseLocationError:
                pass  # fall through and report it below, once, with the reason
        check = db_available()
        if not check.ok:
            print(f"FAIL database  {check.detail}", file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"ok   database  {check.detail}")
        return 0

    results = run_checks()
    failed = [c for c in results if not c.ok]
    for check in results:
        if args.quiet and check.ok:
            continue
        mark = "FAIL" if not check.ok else ("warn" if check.warn else "ok  ")
        print(f"{mark} {check.name:<11} {check.detail}")
    if failed:
        print(f"\n{len(failed)} of {len(results)} checks failed.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
