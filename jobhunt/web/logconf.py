"""uvicorn logging that rotates itself.

launchd holds the fd behind `StandardOutPath` open for the whole life of the
process, which makes the file impossible to rotate from outside: `mv` leaves the
agent writing to an unlinked inode (the bug that was in `discover.sh`), and
truncating leaves a NUL hole where the old offset was. A long-running `KeepAlive`
job therefore has to own its own log.

So the plists point `StandardOutPath` at a small `*.launchd.log` — which catches
tracebacks raised before logging exists and should stay near-empty — and
everything after startup goes here, through a handler that reopens its own fd.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .. import config

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

# ISO 8601 UTC, same as everything written to the DB. The repo has one clock.
logging.Formatter.converter = time.gmtime
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


def dict_config(path: Path | None = None) -> dict[str, Any]:
    """A `logging.config.dictConfig` for `uvicorn.run(log_config=...)`."""
    target = path or (config.LOG_DIR / "dashboard.log")
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(target),
        "maxBytes": MAX_BYTES,
        "backupCount": BACKUP_COUNT,
        "encoding": "utf-8",
    }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": _FORMAT, "datefmt": _DATEFMT},
            # use_colors=False: escape codes in a file are noise, and `grep` on a
            # log full of them is worse than useless.
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s %(levelname)s %(client_addr)s "%(request_line)s" %(status_code)s',
                "datefmt": _DATEFMT,
                "use_colors": False,
            },
        },
        "handlers": {
            "default": {**handler, "formatter": "default"},
            "access": {**handler, "formatter": "access"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["default"], "level": "INFO"},
    }
