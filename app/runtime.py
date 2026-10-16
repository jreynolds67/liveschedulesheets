"""Process setup shared by both entry points: the local time zone and logging.

The local zone comes from the TZ env var through zoneinfo (backed by the
pinned `tzdata` package), not the C library: slim base images may ship
without /usr/share/zoneinfo, in which case TZ would be silently ignored and
"local time" would quietly be UTC.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _local_tz() -> tzinfo:
    name = (os.environ.get("TZ") or "").lstrip(":").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logging.getLogger(__name__).warning("Unknown TZ %r; using UTC", name)
    return timezone.utc


LOCAL_TZ = _local_tz()


class _LocalFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        dt = datetime.fromtimestamp(record.created, LOCAL_TZ)
        return dt.strftime(datefmt) if datefmt else dt.strftime("%Y-%m-%d %H:%M:%S %Z")


LOG_FILENAME = "liveschedulesheets.log"
# The log file on the /data volume: kept across redeploys (Docker's own log
# goes with the container), up to _LOG_FILES files of _LOG_BYTES each.
_LOG_BYTES = 5 * 1024 * 1024
_LOG_FILES = 5


def setup_logging(log_dir: str = "") -> None:
    """Log to stderr (Docker's log), and to `log_dir`/liveschedulesheets.log
    when given, so what the tool did is still there after a redeploy."""
    fmt = _LocalFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    problem = None
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, LOG_FILENAME), maxBytes=_LOG_BYTES,
                backupCount=_LOG_FILES - 1, encoding="utf-8"))
        except OSError as exc:
            problem = f"Can't write the log file in {log_dir}: {exc}"
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        handlers=handlers,
    )
    if problem:
        logging.getLogger(__name__).warning("%s", problem)


# What the activity view keeps with "changes only": the tool changing LSP or
# its own tracking, and alerts.
_CHANGES = re.compile(r": (Created|Updated|Removed|Locked|Unlocked|Set|Sent|Not changing) [\"']"
                      r"|: Deleted |Alert sent|Dry run turned|tracking it")


def read_log_tail(log_dir: str, max_lines: int = 300, changes_only: bool = False) -> list[str]:
    """The last `max_lines` lines of the log file (oldest first)."""
    path = os.path.join(log_dir, LOG_FILENAME)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 2 * 1024 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    if changes_only:
        lines = [ln for ln in lines if _CHANGES.search(ln)]
    return lines[-max_lines:]
