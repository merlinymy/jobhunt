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
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, queries, states

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


def _submitted_count() -> int | None:
    """How many applications a lost database would take with it for good.

    None means the question could not be answered. An unreadable database is
    db_available's and db_integrity's failure to report, not this one's — but it
    must not be reported as a zero, which would read as "nothing to lose".
    """
    try:
        conn = db.connect()
    except Exception:
        return None
    try:
        counts = queries.state_counts(conn)
    except Exception:
        return None
    finally:
        conn.close()
    return sum(n for state, n in counts.items() if state in states.SUBMITTED_STATES)


def backups() -> Check:
    # Never having taken a backup is a different condition from a backup regime
    # that has broken, and they deserve different severities. A fresh install has
    # no backup directory and does not need one yet — failing there trains people
    # to ignore this command. Once an application has actually been submitted that
    # stops being true on any machine, so the severity follows the data rather than
    # the hostname.
    #
    # The warn wording states a fact ("no submitted applications yet") rather than
    # judging the database worthless. Losing it would still cost an ingest and a
    # rescore, and generated narrative answers accumulate in the DB only — see the
    # `docs/profile/` invariant in CLAUDE.md. Those are not counted here; if that
    # starts to matter, widen `at_risk`, do not widen the claim.
    missing = None
    if not config.BACKUP_DIR.is_dir():
        missing = f"{config.BACKUP_DIR} does not exist"
    elif not sorted(config.BACKUP_DIR.glob("jobhunt-*.db")):
        missing = f"no snapshots in {config.BACKUP_DIR}"
    if missing:
        at_risk = _submitted_count()
        if at_risk is None:
            return Check(
                "backups",
                True,
                f"{missing}; could not read the database to tell whether that matters",
                warn=True,
            )
        if at_risk:
            return Check(
                "backups",
                False,
                f"{missing} — and {at_risk} submitted application(s) would be "
                f"unrecoverable. Run `make backup`.",
            )
        return Check(
            "backups",
            True,
            f"{missing} — no submitted applications yet, so this is not urgent",
            warn=True,
        )
    failed = sorted(config.BACKUP_DIR.glob("*.FAILED"))
    snapshots = sorted(config.BACKUP_DIR.glob("jobhunt-*.db"))
    newest = max(snapshots, key=lambda p: p.stat().st_mtime)
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    detail = f"{len(snapshots)} snapshots, newest {age_h:.1f}h old ({_human(newest.stat().st_size)})"
    if failed:
        return Check("backups", False, f"{detail}; {len(failed)} FAILED left in place")
    if age_h > STALE_BACKUP_HOURS:
        return Check("backups", False, f"{detail} — the nightly agent has missed a run")
    return Check("backups", True, detail)


DISCOVER_LABEL = "com.jobhunt.discover"


def agent_loaded(label: str = DISCOVER_LABEL) -> bool | None:
    """Is this launchd agent loaded in the current GUI session?

    None means the question could not be answered — no `launchctl`, no Aqua
    session, or not macOS at all. Callers must not read that as a no.
    """
    try:
        done = subprocess.run(
            ["launchctl", "list", label], capture_output=True, timeout=5
        )
    except Exception:
        return None
    return done.returncode == 0


def discovery_status() -> list[tuple[int, str]]:
    """Where new postings come from, as (log level, message) pairs.

    Called at dashboard startup. Nothing in the UI says how jobs arrive, so an
    uninstalled discovery agent looks identical to a quiet job market: the queue
    simply stops growing and there is no page that would tell you why.
    """
    out: list[tuple[int, str]] = []
    newest: str | None = None
    waiting: int | None = None
    try:
        conn = db.connect()
    except Exception:
        conn = None
    if conn is not None:
        try:
            newest = conn.execute("SELECT max(discovered_at) FROM jobs").fetchone()[0]
            waiting = conn.execute(
                "SELECT count(*) FROM applications WHERE state = ?", (states.SCORED,)
            ).fetchone()[0]
        except Exception:
            pass
        finally:
            conn.close()

    if newest:
        try:
            age_h = (
                datetime.now(timezone.utc) - datetime.fromisoformat(newest)
            ).total_seconds() / 3600
            when = f"{age_h:.0f}h ago" if age_h < 48 else f"{age_h / 24:.0f} days ago"
        except ValueError:
            when = newest
    else:
        when = "never"
    queue = "" if waiting is None else f", {waiting} scored and waiting in /review"
    out.append((logging.INFO, f"discovery: last posting found {when}{queue}"))

    if agent_loaded() is False:
        out.append((
            logging.WARNING,
            "discovery: no launchd agent installed, so nothing finds new postings on "
            "its own — run `make ingest && make score` by hand, or "
            "`./deploy/install.sh` to schedule it for 06:30 and 18:30 daily",
        ))
    return out


def discovery_runs() -> Check:
    """Did the last sweep and the last scoring run actually work?

    A failed 06:30 agent is otherwise invisible: nothing crashes, the queue just
    stops growing, and the log that says why is one nobody reads on a good day.
    A warning rather than a failure — a dead source is a bad week, not a broken
    install, and this check has to stay worth reading.
    """
    from . import runs

    try:
        conn = db.connect()
    except Exception as exc:
        return Check("runs", False, f"cannot open the database: {exc}")
    try:
        live = runs.active(conn)
        if live is not None:
            return Check(
                "runs", True,
                f"{live['task']} running now ({live['trigger']}, started "
                f"{runs.ago(live['started_at'])} ago)",
            )
        parts, unhappy = [], False
        for task in runs.TASKS:
            row = runs.latest(conn, task)
            if row is None:
                parts.append(f"{task}: never run")
                continue
            parts.append(f"{task}: {row['state']} {runs.ago(row['finished_at'])} ago")
            unhappy = unhappy or row["state"] != "done"
        return Check("runs", True, " · ".join(parts), warn=unhappy)
    finally:
        conn.close()


def corpus_metrics() -> Check:
    """Which `bullets.metric` values are being withheld from the tailor.

    A number silently dropped from every resume is worse than a bad number
    printed on one, because you cannot argue with what you cannot see. This is
    the list to argue with — and the list to fix in `experience.yaml`, since a
    metric worth withholding is usually a metric worth replacing.
    """
    from . import tailor

    try:
        conn = db.connect()
    except Exception as exc:
        return Check("metrics", False, f"cannot open the database: {exc}")
    try:
        total = conn.execute(
            "SELECT count(*) FROM bullets WHERE metric IS NOT NULL AND metric != ''"
        ).fetchone()[0]
        weak = tailor.weak_metrics(conn)
    finally:
        conn.close()

    if not total:
        return Check("metrics", True, "no metrics set on any bullet")
    if not weak:
        return Check("metrics", True, f"all {total} metrics look worth quoting")
    detail = (
        f"{len(weak)} of {total} metrics count artifacts rather than results and are "
        f"withheld from the tailor — bullets "
        + ", ".join(str(bid) for bid, _ in weak[:12])
        + (f" and {len(weak) - 12} more" if len(weak) > 12 else "")
        + ". `python -m jobhunt.tailor --metrics` lists them."
    )
    # A warning, never a failure. It is a judgement about resume writing, and
    # the corpus is hand-maintained on purpose.
    return Check("metrics", True, detail, warn=True)


def extras() -> Check:
    """The optional deps are imported lazily inside functions, so a venv missing
    them installs clean and then dies at 06:30 on `import jobspy`."""
    wanted = {"anthropic": "llm", "jobspy": "ingest", "rendercv": "resume",
              "docx": "resume", "pypdf": "resume"}
    missing = [m for m in wanted if importlib.util.find_spec(m) is None]
    if missing:
        groups = ",".join(sorted({wanted[m] for m in missing}))
        return Check(
            "extras", False, f"missing {missing} — uv pip install -e '.[{groups}]'"
        )
    return Check("extras", True, f"{', '.join(sorted(wanted))} importable")


def libreoffice() -> Check:
    """The .docx -> submission PDF step shells out to LibreOffice (Phase 2 choice).

    A host dependency, not a Python one, so it is checked separately. Without it a
    packet build cannot produce the PDF that carries the §8 work-auth line — and
    the code refuses to fall back to a renderer that would drop it.
    """
    from . import docx_render

    found = docx_render.find_soffice()
    if found is None:
        return Check(
            "libreoffice", False,
            "soffice not found — the .docx -> PDF step needs it. "
            "brew install --cask libreoffice",
        )
    return Check("libreoffice", True, found)


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
    discovery_runs,
    corpus_metrics,
    extras,
    libreoffice,
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
