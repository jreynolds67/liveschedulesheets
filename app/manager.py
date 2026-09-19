"""Runtime manager: owns the background sync loop and the operations the web
UI calls (preview, run-now, channels, connection test).

Components (sheet reader, LSP client, syncer) are rebuilt only when the config
actually changes, so the LSP auth token is reused across passes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .config import Config, ConfigError, parse_config
from .lsp_client import LspClient, LspError
from .settings_store import SettingsStore
from .sheets import SheetReader
from .state import State
from .sync import Syncer

log = logging.getLogger(__name__)


class ManagerError(Exception):
    pass


class SyncManager:
    def __init__(self, store: SettingsStore):
        self.store = store
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._cache_hash: Optional[str] = None
        self._cfg: Optional[Config] = None
        self._syncer: Optional[Syncer] = None

        self.last_run: Optional[dict] = None
        self.last_error: Optional[str] = None

    # -- component build (cached by config hash) ---------------------------

    def _components(self) -> tuple[Config, Syncer]:
        raw = self.store.load()
        h = hashlib.sha1(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        if h != self._cache_hash or self._syncer is None:
            cfg = parse_config(raw)  # raises ConfigError if invalid
            reader = SheetReader(cfg)
            lsp = LspClient(cfg.lsp)
            state = State(cfg.runtime.state_file)
            self._cfg, self._syncer = cfg, Syncer(cfg, reader, lsp, state)
            self._cache_hash = h
            log.info("Rebuilt sync components from updated config")
        return self._cfg, self._syncer

    # -- operations for the web UI -----------------------------------------

    def preview(self) -> list[dict]:
        with self._lock:
            _, syncer = self._components()
            return [item.to_dict() for item in syncer.plan()]

    def run_now(self) -> dict:
        with self._lock:
            _, syncer = self._components()
            summary = syncer.run_once()
        self.last_run = {"time": _now_iso(), "summary": summary, "trigger": "manual"}
        self.last_error = None
        return summary

    def channels(self) -> list[dict]:
        with self._lock:
            _, syncer = self._components()
            chans = syncer.lsp.get_all_channels()
        return [{"Id": c.get("Id"), "Name": c.get("Name")} for c in chans]

    def tabs(self) -> list[dict]:
        """Visible tabs in the sheet with their current UI state."""
        with self._lock:
            cfg, syncer = self._components()
            visible = syncer.reader._visible_tabs()
        allow = set(cfg.sheet.tabs)
        out = []
        for name in visible:
            ov = cfg.tab_overrides.get(name)
            out.append({
                "name": name,
                "enabled": (True if ov is None else ov.enabled)
                           and (name in allow if allow else True),
                "default_control_room": (ov.default_control_room if ov else None),
                "in_allow_list": (name in allow) if allow else True,
            })
        return out

    def test_connection(self) -> dict:
        try:
            chans = self.channels()
            return {"ok": True, "channel_count": len(chans),
                    "channels": [c["Name"] for c in chans if c.get("Name")]}
        except (LspError, ConfigError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def status(self) -> dict:
        cfg_ok, cfg_err = True, None
        interval = None
        dry_run = None
        try:
            cfg, _ = self._components()
            interval = cfg.runtime.poll_interval_seconds
            dry_run = cfg.runtime.dry_run
        except ConfigError as exc:
            cfg_ok, cfg_err = False, str(exc)
        return {
            "config_ok": cfg_ok,
            "config_error": cfg_err,
            "poll_interval_seconds": interval,
            "dry_run": dry_run,
            "loop_running": bool(self._thread and self._thread.is_alive()),
            "last_run": self.last_run,
            "last_error": self.last_error,
        }

    # -- background loop ----------------------------------------------------

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sync-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("Background sync loop started")
        while not self._stop.is_set():
            interval = 900
            try:
                with self._lock:
                    cfg, syncer = self._components()
                    interval = cfg.runtime.poll_interval_seconds
                    summary = syncer.run_once()
                self.last_run = {"time": _now_iso(), "summary": summary, "trigger": "scheduled"}
                self.last_error = None
                if cfg.runtime.run_once:
                    log.info("run_once set; stopping loop")
                    break
            except ConfigError as exc:
                self.last_error = str(exc)
                log.warning("Config not ready: %s", exc)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Unhandled error during sync pass")

            # Interruptible sleep.
            self._stop.wait(timeout=max(5, interval))
        log.info("Background sync loop stopped")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
