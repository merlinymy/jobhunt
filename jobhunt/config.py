"""Paths, environment, and the one clock.

Timestamps are made here and nowhere else. No `datetime('now')` in scattered SQL.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    """Minimal .env reader. python-dotenv is not worth a dependency for this."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Real environment wins over the file, so a one-off export can override.
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env(REPO_ROOT / ".env")

DB_PATH = Path(os.environ.get("JOBHUNT_DB") or REPO_ROOT / "jobhunt.db").expanduser()
MIGRATIONS_DIR = REPO_ROOT / "migrations"
# Your data, not the repo's. Overridable so a fork can keep its profile outside
# the checkout entirely — the files hold a legal name, a phone number, and a
# contact network of other people, none of which belongs in a public clone.
PROFILE_DIR = Path(
    os.environ.get("JOBHUNT_PROFILE_DIR") or REPO_ROOT / "docs" / "profile"
).expanduser()
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"
PROMPTS_DIR = REPO_ROOT / "config" / "prompts"
OUT_DIR = REPO_ROOT / "out"

# Both on the internal disk, deliberately. Backups on the same volume as the DB
# protect against nothing, and logs have to be writable when that volume is gone
# — an agent that cannot say why it did nothing is worse than one that failed.
BACKUP_DIR = Path(
    os.environ.get("JOBHUNT_BACKUP_DIR")
    or "~/Library/Application Support/jobhunt/backups"
).expanduser()
LOG_DIR = Path(os.environ.get("JOBHUNT_LOG_DIR") or "~/Library/Logs/jobhunt").expanduser()

HOST = os.environ.get("JOBHUNT_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOBHUNT_PORT", "8000"))


def utcnow() -> str:
    """ISO 8601 UTC, second precision, stored as TEXT."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    """UTC date, for date-only form defaults."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def date_to_utc(value: str) -> str:
    """Turn a `YYYY-MM-DD` form field into a timestamp, or raise.

    Without this, a junk date string was being interpolated straight into the
    column — `applied_at = 'garbageT12:00:00Z'` — which is unparseable, silently
    corrupts every date-ordered query, and violates the ISO 8601 rule. Midday is
    used because a date field carries no time and midnight straddles timezones.
    """
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"expected a date as YYYY-MM-DD, got {value!r}") from exc
    return parsed.strftime("%Y-%m-%dT12:00:00Z")


def to_utc_timestamp(value: str) -> str:
    """Normalize a date or ISO 8601 timestamp to the stored TEXT form, or raise.

    `jobs.posted_at` is the second field that took a user string straight to the
    column — `'not-a-date'` stored cleanly, which poisons the posted-to-discovered
    lag that the aggregator-staleness check reads. Unlike `date_to_utc` this also
    accepts a full timestamp, because Phase 4 ingest gets both shapes back from
    aggregators: `2026-07-15` from one, `2026-07-15T09:30:00+02:00` from another.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("expected a date or timestamp, got nothing")
    if len(text) == 10:  # bare date; midday, for the reason in date_to_utc
        return date_to_utc(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"expected an ISO 8601 date or timestamp, got {text!r}"
        ) from exc
    if parsed.tzinfo is None:  # naive means UTC here, never local
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(days: int) -> str:
    """Cutoff timestamp for rolling windows, so no query inlines datetime('now')."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
