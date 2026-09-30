"""Process setup shared by both entry points: the local time zone and logging.

The local zone comes from the TZ env var through zoneinfo (backed by the
pinned `tzdata` package), not the C library: slim base images may ship
without /usr/share/zoneinfo, in which case TZ would be silently ignored and
"local time" would quietly be UTC.
"""
from __future__ import annotations

import logging
import os
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


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_LocalFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        handlers=[handler],
    )
