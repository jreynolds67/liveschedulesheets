"""Headless entry point (no web UI): run the sync loop, or a single pass.

The web UI (`python -m app.webui`) is the default container command and also
runs this loop in-process. Use this module for cron/testing:
    RUN_ONCE=true python -m app.main
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

from .config import ConfigError
from .manager import SyncManager
from .runtime import setup_logging
from .settings_store import SettingsStore
from .webui import CONFIG_PATH, SEED_PATH, SHUTDOWN_WAIT_SECONDS

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    logging.getLogger(__name__).info("Signal %s; shutting down", signum)
    _stop = True


def main() -> int:
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    store = SettingsStore(CONFIG_PATH, SEED_PATH)
    manager = SyncManager(store)

    run_once = os.environ.get("RUN_ONCE", "").strip().lower() in ("1", "true", "yes", "on")
    if run_once:
        try:
            manager.run_now()
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        return 0

    manager.start_loop()
    while not _stop:
        time.sleep(1)
    manager.stop(timeout=SHUTDOWN_WAIT_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
