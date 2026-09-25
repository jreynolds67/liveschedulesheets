"""Runtime manager: owns the background sync loop and the operations the web
UI calls (preview, run-now, channels, connection test, sheet inspection).

Components (sheet reader, LSP client, syncer) are rebuilt only when the config
actually changes, so the LSP auth token is reused across passes. Sheet
inspection (tab list, row mapping) uses its own reader built from just the
sheet half of the config, so it works before the LSP login is set up.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .config import (Config, ConfigError, extract_spreadsheet_id, parse_config,
                     parse_lsp_settings, parse_row_picks, parse_sheet_settings)
from .lsp_client import LspClient, LspError
from .settings_store import SettingsStore
from .sheets import SheetReader, inspect_grid
from .state import State
from .sync import Syncer

log = logging.getLogger(__name__)

# How long a fetched tab is reused while the operator adjusts row picks.
_GRID_CACHE_SECONDS = 120


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
        # LSP-only syncer, used while the sheet side (e.g. Google key) isn't set up.
        self._lsp_hash: Optional[str] = None
        self._lsp_only: Optional[Syncer] = None

        self.last_run: Optional[dict] = None
        self.last_error: Optional[str] = None

        # Sheet inspection (web UI) -- separate lock so it never waits on a sync.
        self._sheet_lock = threading.Lock()
        self._sheet_reader_key: Optional[tuple] = None
        self._sheet_reader_obj: Optional[SheetReader] = None
        self._grid_cache: dict[tuple, tuple[float, list]] = {}

    # -- component build (cached by config hash) ---------------------------

    def _components(self) -> tuple[Config, Syncer]:
        raw = self.store.load()
        h = hashlib.sha1(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        if h != self._cache_hash or self._syncer is None:
            cfg = parse_config(raw)  # raises ConfigError if invalid
            try:
                reader = SheetReader(cfg)
            except Exception as exc:  # noqa: BLE001 -- e.g. a malformed key file
                raise ConfigError(f"Could not load the Google service-account key: {exc}") from exc
            lsp = LspClient(cfg.lsp)
            state = State(cfg.runtime.state_file)
            self._cfg, self._syncer = cfg, Syncer(cfg, reader, lsp, state)
            self._cache_hash = h
            self._lsp_only = self._lsp_hash = None
            log.info("Rebuilt sync components from updated config")
        return self._cfg, self._syncer

    def _lsp_syncer(self) -> Syncer:
        """Syncer for LSP-only operations (channels, scheduled view, cleanup).

        Uses the full syncer when the whole config is valid; otherwise falls
        back to one built from just the LSP half, with no sheet reader, so LSP
        can be tested before the Google key is installed.
        """
        try:
            return self._components()[1]
        except ConfigError as full_exc:
            raw = self.store.load()
            try:
                ls = parse_lsp_settings(raw)
            except ConfigError:
                raise full_exc from None
            h = hashlib.sha1(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
            if h != self._lsp_hash or self._lsp_only is None:
                self._lsp_only = Syncer(ls, None, LspClient(ls.lsp), State(ls.runtime.state_file))
                self._lsp_hash = h
            return self._lsp_only

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
            syncer = self._lsp_syncer()
            chans = syncer.lsp.get_all_channels()
        return [{"Id": c.get("Id"), "Name": c.get("Name")} for c in chans]

    def created_events(self) -> list[dict]:
        with self._lock:
            syncer = self._lsp_syncer()
            return syncer.created_events()

    def scheduled_events(self, past_days: int = 0) -> dict:
        with self._lock:
            syncer = self._lsp_syncer()
            return syncer.scheduled_events(past_days)

    def delete_created(self) -> dict:
        with self._lock:
            syncer = self._lsp_syncer()
            return syncer.delete_created()

    # -- sheet inspection (web UI row mapping) -------------------------------

    def _sheet_reader(self, spreadsheet: Optional[str]) -> SheetReader:
        """Reader for the saved sheet, or for `spreadsheet` (a link or ID the
        operator just pasted, not yet saved). Caller holds _sheet_lock."""
        raw = copy.deepcopy(self.store.load())
        if spreadsheet:
            sid = extract_spreadsheet_id(spreadsheet)
            if not sid:
                raise ConfigError("That doesn't look like a Google Sheets link or ID")
            raw.setdefault("sheet", {})["spreadsheet_id"] = sid
        settings = parse_sheet_settings(raw)
        key = (settings.google_credentials_file, settings.sheet.spreadsheet_id)
        if key != self._sheet_reader_key or self._sheet_reader_obj is None:
            self._sheet_reader_obj = SheetReader(settings)
            self._sheet_reader_key = key
        else:
            self._sheet_reader_obj.cfg = settings  # labels/overrides may have changed
        return self._sheet_reader_obj

    def _grid(self, reader: SheetReader, tab: str, refresh: bool) -> list:
        key = (reader.cfg.sheet.spreadsheet_id, tab)
        hit = self._grid_cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < _GRID_CACHE_SECONDS:
            return hit[1]
        grid = reader.fetch_grid(tab)
        self._grid_cache[key] = (time.monotonic(), grid)
        return grid

    def tabs(self, spreadsheet: Optional[str] = None) -> dict:
        """The sheet's title and its visible tabs with their current UI state."""
        with self._sheet_lock:
            reader = self._sheet_reader(spreadsheet)
            try:
                meta = reader.sheet_meta()
            except Exception as exc:  # noqa: BLE001
                if any(code in str(exc) for code in ("403", "404")):
                    raise ManagerError(
                        "Google can't open that sheet. Check the link, and share the sheet "
                        f"(Viewer) with {_service_account_email(reader.cfg)}."
                    ) from exc
                raise
            cfg = reader.cfg
        allow = set(cfg.sheet.tabs)
        out = []
        for t in meta["tabs"]:
            if t["hidden"]:
                continue
            name = t["name"]
            ov = cfg.tab_overrides.get(name)
            out.append({
                "name": name,
                "gid": t["gid"],
                "enabled": (True if ov is None else ov.enabled)
                           and (name in allow if allow else True),
                "default_control_room": (ov.default_control_room if ov else None),
                "in_allow_list": (name in allow) if allow else True,
                "row_picks": len(ov.rows) if ov else 0,
            })
        return {"spreadsheet_id": cfg.sheet.spreadsheet_id, "title": meta["title"], "tabs": out}

    def inspect_tab(self, tab: str, spreadsheet: Optional[str] = None,
                    rows: Optional[dict] = None, default_room: Optional[str] = None,
                    refresh: bool = False) -> dict:
        """Sheet preview + row detection for one tab. `rows` / `default_room`
        are the operator's unsaved choices (None = use what is saved)."""
        with self._sheet_lock:
            reader = self._sheet_reader(spreadsheet)
            grid = self._grid(reader, tab, refresh)
            cfg = reader.cfg
        picks = parse_row_picks(rows) if rows is not None else None
        return inspect_grid(tab, grid, cfg, picks=picks, default_room=default_room)

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
        created_count = 0
        try:
            cfg, syncer = self._components()
            interval = cfg.runtime.poll_interval_seconds
            dry_run = cfg.runtime.dry_run
            created_count = len(syncer.state.tool_created())
        except ConfigError as exc:
            cfg_ok, cfg_err = False, str(exc)
            try:
                created_count = len(self._lsp_syncer().state.tool_created())
            except ConfigError:
                pass
        return {
            "config_ok": cfg_ok,
            "config_error": cfg_err,
            "poll_interval_seconds": interval,
            "dry_run": dry_run,
            "created_count": created_count,
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


def _service_account_email(cfg) -> str:
    try:
        with open(cfg.google_credentials_file, encoding="utf-8") as fh:
            return json.load(fh).get("client_email") or "the service account"
    except (OSError, ValueError):
        return "the service account"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
