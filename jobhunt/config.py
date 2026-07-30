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
PROFILE_DIR = REPO_ROOT / "docs" / "profile"
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"
OUT_DIR = REPO_ROOT / "out"

HOST = os.environ.get("JOBHUNT_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOBHUNT_PORT", "8000"))


def utcnow() -> str:
    """ISO 8601 UTC, second precision, stored as TEXT."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    """UTC date, for date-only form defaults."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_ago(days: int) -> str:
    """Cutoff timestamp for rolling windows, so no query inlines datetime('now')."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
