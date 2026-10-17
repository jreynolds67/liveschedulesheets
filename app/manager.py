"""Runtime manager: owns the background sync loop and the operations the web
UI calls (preview, run-now, channels, connection test, sheet inspection).

Components (sheet reader, LSP client, syncer) are rebuilt only when the config
actually changes, so the LSP auth token is reused across passes. Sheet
inspection (tab list, row mapping) uses its own reader built from just the
sheet half of the config, so it works before the LSP login is set up.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .alerts import FAILS_BEFORE_ALERT, Alerter
from .config import (Config, ConfigError, extract_spreadsheet_id, parse_config,
                     parse_lsp_settings, parse_row_picks, parse_sheet_settings)
from .lsp_client import LspClient, LspError
from .settings_store import SettingsStore
from .sheets import SheetReader, inspect_grid
from .state import State, atomic_write
from .sync import Syncer

log = logging.getLogger(__name__)

# How long a fetched tab is reused while the operator adjusts row picks.
_GRID_CACHE_SECONDS = 120
# How long a UI request waits for a running sync pass before giving up, so
# slow LSP replies can't tie up every web server thread: quick looks...
_READ_WAIT_SECONDS = 15
# ...and actions the operator asked for and expects to happen.
_WRITE_WAIT_SECONDS = 120
# The loop counts as stuck when a pass makes no progress for this much longer
# than the interval.
_STUCK_SECONDS = 30 * 60
# How soon the loop tries again after a configuration problem.
_CONFIG_RETRY_SECONDS = 60
# How often the watchdog checks the loop.
_WATCHDOG_SECONDS = 60
# Syncing counts as broken (/healthz/sync) once the last clean pass is this
# many intervals old, and at least _STALE_MIN_SECONDS.
_STALE_INTERVALS = 3
_STALE_MIN_SECONDS = 45 * 60
# Status file next to config.yaml: the last pass and what alerts were sent.
STATUS_FILENAME = "status.json"


class ManagerError(Exception):
    pass


class SyncManager:
    def __init__(self, store: SettingsStore,
                 on_unhealthy: Optional[Callable[[str], None]] = None):
        self.store = store
        # Called (once) when the loop has died or is stuck, to restart the
        # process: Docker's restart policy ignores an unhealthy container.
        self._on_unhealthy = on_unhealthy
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Set when the settings are saved, so the loop re-reads the interval.
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._cache_hash: Optional[str] = None
        self._cfg: Optional[Config] = None
        self._syncer: Optional[Syncer] = None
        # LSP-only syncer, used while the sheet side (e.g. Google key) isn't set up.
        self._lsp_hash: Optional[str] = None
        self._lsp_only: Optional[Syncer] = None
        # The one in-memory copy of the state file, shared by both syncers so
        # neither acts on records the other has changed.
        self._state: Optional[State] = None

        self._created_count = 0  # for status() while a sync holds the lock
        self._state_error: Optional[str] = None
        self._started = time.time()
        # The last pass, the last one that ran without errors (epoch
        # seconds), and the alerts sent: kept in the status file so a
        # restart doesn't blank the Status card or repeat alerts.
        saved = self._read_status()
        self.last_run: Optional[dict] = saved.get("last_run")
        self.last_error: Optional[str] = saved.get("last_error")
        self.last_clean: Optional[float] = saved.get("last_clean")
        self._last_ok: Optional[bool] = saved.get("last_ok")
        self._alerter = Alerter(saved.get("alerts"))
        self._result_lock = threading.Lock()
        # Loop liveness, for the container health check.
        self._heartbeat = time.monotonic()
        self._interval = 900

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
            lsp = LspClient(cfg.lsp, read_only=cfg.runtime.dry_run)
            state = self._load_state(cfg.runtime.state_file)
            self._cfg, self._syncer = cfg, Syncer(cfg, reader, lsp, state, stop=self._stop,
                                                  heartbeat=self._beat)
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
                self._lsp_only = Syncer(ls, None, LspClient(ls.lsp, read_only=ls.runtime.dry_run),
                                        self._load_state(ls.runtime.state_file), stop=self._stop,
                                        heartbeat=self._beat)
                self._lsp_hash = h
            return self._lsp_only

    def _load_state(self, path: str) -> State:
        """The state for `path`: the copy already in memory (every change is
        saved as it's made, so it is never behind the file), or read afresh."""
        if self._state is not None and self._state.path == path:
            return self._state
        try:
            state = State(path, exclusive=True)
        except OSError as exc:
            raise ConfigError(f"Could not read the state file {path}: {exc}") from exc
        if state.load_error:
            self._state_error = state.load_error
        self._state = state
        return state

    def _beat(self) -> None:
        self._heartbeat = time.monotonic()

    @contextlib.contextmanager
    def _locked(self, wait: float):
        """Hold the sync lock, waiting at most `wait` seconds for a pass."""
        if not self._lock.acquire(timeout=wait):
            raise ManagerError("A sync pass is running; try again in a moment")
        try:
            yield
        finally:
            self._lock.release()

    def _finished(self, summary: dict, trigger: str) -> None:
        self.last_run = {"time": _now_iso(), "summary": summary, "trigger": trigger}
        # Problems that didn't raise (sheet unreadable, a tab failing, LSP
        # unreachable) still belong in the status card's error line.
        self.last_error = "; ".join(summary.get("problems") or []) or None
        ok = not summary.get("errors")
        self._pass_result(ok, self.last_error or "errors in the last pass",
                          summary.get("warnings") if "warnings" in summary else None)

    def _pass_result(self, ok: bool, why: str, warnings: Optional[list]) -> None:
        """Record how a pass went, alert if that's news, and save it. (Run
        now and the loop can both finish a pass at once: one at a time.)"""
        with self._result_lock:
            self._last_ok = ok
            if ok:
                self.last_clean = time.time()
            try:
                self._alerter.pass_done(self._webhook(), ok, why, warnings)
            except Exception:  # noqa: BLE001 - an alert must never break the loop
                log.exception("Alerting failed")
            self._write_status()

    def _webhook(self) -> str:
        try:
            return str(((self.store.load().get("runtime") or {}).get("alert_webhook_url")
                        or "")).strip()
        except (ConfigError, OSError):
            return ""

    def send_test_alert(self) -> dict:
        url = self._webhook()
        if not url:
            return {"ok": False, "error": "Set and save an alert webhook URL first"}
        if not self._alerter.send(url, "LiveScheduleSheets test alert: alerts reach this channel."):
            return {"ok": False, "error": "The webhook didn't accept it; see the log"}
        return {"ok": True}

    @property
    def _status_path(self) -> str:
        return os.path.join(os.path.dirname(self.store.path) or ".", STATUS_FILENAME)

    def _read_status(self) -> dict:
        try:
            with open(self._status_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_status(self) -> None:
        data = {"last_run": self.last_run, "last_error": self.last_error,
                "last_clean": self.last_clean, "last_ok": self._last_ok,
                "alerts": self._alerter.to_dict()}
        try:
            atomic_write(self._status_path, lambda fh: json.dump(data, fh, indent=2, default=str))
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Could not save %s: %s", self._status_path, exc)

    def sync_health(self) -> tuple[bool, str]:
        """For monitoring (/healthz/sync): is syncing actually working? Not
        when the last pass had errors, or no pass has run cleanly for a while
        (Google key revoked, LSP login changed, LSP down...). Unlike
        health(), this never restarts anything: a restart can't fix those."""
        stale_after = max(_STALE_INTERVALS * self._interval, _STALE_MIN_SECONDS)
        # As with alerts, one failed pass (an LSP blip) isn't "broken" yet.
        if self._last_ok is False and self._alerter.fails >= FAILS_BEFORE_ALERT:
            return False, (f"the last {self._alerter.fails} syncs failed: "
                           f"{self.last_error or 'see the Status card'}")
        if self.last_clean is None:
            if time.time() - self._started < stale_after:
                return True, "starting: no sync pass yet"
            return False, "no sync pass has run without errors"
        age = time.time() - self.last_clean
        if age > stale_after:
            return False, f"no sync pass without errors for {int(age // 60)} minutes"
        return True, "ok"

    # -- operations for the web UI -----------------------------------------

    def preview(self) -> dict:
        with self._locked(_READ_WAIT_SECONDS):
            _, syncer = self._components()
            items = [item.to_dict() for item in syncer.plan()]
            return {"items": items, "warnings": [w["text"] for w in syncer.sheet_warnings],
                    "errors": list(syncer.sheet_errors)}

    def run_now(self) -> dict:
        with self._locked(_WRITE_WAIT_SECONDS):
            _, syncer = self._components()
            summary = syncer.run_once()
        self._finished(summary, "manual")
        return summary

    def send_event(self, tab: str, event_date: str, event_name: str, occurrence: int = 0) -> dict:
        with self._locked(_WRITE_WAIT_SECONDS):
            _, syncer = self._components()
            return syncer.send_one(tab, event_date, event_name, occurrence)

    def create_test_event(self, name: str, pcr: str, start: datetime, minutes: int) -> dict:
        with self._locked(_WRITE_WAIT_SECONDS):
            return self._lsp_syncer().create_test_event(name, pcr, start, minutes)

    def unlock_event(self, record_id: str) -> dict:
        with self._locked(_WRITE_WAIT_SECONDS):
            _, syncer = self._components()
            return syncer.unlock(record_id)

    def unlock_all(self) -> dict:
        with self._locked(_WRITE_WAIT_SECONDS):
            _, syncer = self._components()
            return syncer.unlock_all()

    def channels(self) -> list[dict]:
        return self._channels()[0]

    def _channels(self) -> tuple[list[dict], Optional[str]]:
        with self._locked(_READ_WAIT_SECONDS):
            syncer = self._lsp_syncer()
            chans = syncer.lsp.get_all_channels()
            auth_mode = syncer.lsp.auth_mode
        return [{"Id": c.get("Id"), "Name": c.get("Name")} for c in chans], auth_mode

    def event_name_channels(self) -> dict:
        """Whether each PCR channel's events carry the event-name variable."""
        with self._locked(_READ_WAIT_SECONDS):
            return self._lsp_syncer().event_name_channels()

    def lsp_event(self, event_id: str) -> Optional[dict]:
        with self._locked(_READ_WAIT_SECONDS):
            return self._lsp_syncer().lsp.get_event(event_id)

    def scheduled_events(self, past_days: int = 0) -> dict:
        with self._locked(_READ_WAIT_SECONDS):
            syncer = self._lsp_syncer()
            return syncer.scheduled_events(past_days)

    def delete_created(self) -> dict:
        with self._locked(_WRITE_WAIT_SECONDS):
            syncer = self._lsp_syncer()
            return syncer.delete_created()

    def delete_events(self, event_ids: list[str]) -> dict:
        with self._locked(_WRITE_WAIT_SECONDS):
            syncer = self._lsp_syncer()
            return syncer.delete_events(event_ids)

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
        now = time.monotonic()
        self._grid_cache = {k: v for k, v in self._grid_cache.items()
                            if now - v[0] < _GRID_CACHE_SECONDS}
        self._grid_cache[key] = (now, grid)
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
        # Tabs the tool has read on this sheet: one it hasn't (added or
        # renamed since) is off until turned on (see SheetReader.read_events).
        known = self._state.known_tabs(cfg.sheet.spreadsheet_id) if self._state else None
        out = []
        for t in meta["tabs"]:
            if t["hidden"]:
                continue
            name = t["name"]
            ov = cfg.tab_overrides.get(name)
            new = known is not None and not allow and name not in known
            out.append({
                "name": name,
                "gid": t["gid"],
                "new": new,
                "enabled": (ov.enabled if ov is not None else not new)
                           and (name in allow if allow else True),
                "in_allow_list": (name in allow) if allow else True,
                "row_picks": len(ov.rows) if ov else 0,
            })
        return {"spreadsheet_id": cfg.sheet.spreadsheet_id, "title": meta["title"], "tabs": out,
                # every tab, hidden ones too: settings for others are dropped
                "all_tabs": [t["name"] for t in meta["tabs"]]}

    def inspect_tab(self, tab: str, spreadsheet: Optional[str] = None,
                    rows: Optional[dict] = None, refresh: bool = False) -> dict:
        """Sheet preview + row detection for one tab. `rows` are the
        operator's unsaved row picks (None = use what is saved)."""
        with self._sheet_lock:
            reader = self._sheet_reader(spreadsheet)
            grid = self._grid(reader, tab, refresh)
            cfg = reader.cfg
        picks = parse_row_picks(rows) if rows is not None else None
        return inspect_grid(tab, grid, cfg, picks=picks)

    def test_connection(self) -> dict:
        try:
            chans, auth_mode = self._channels()
            return {"ok": True, "channel_count": len(chans), "auth_mode": auth_mode,
                    "channels": [c["Name"] for c in chans if c.get("Name")]}
        except (LspError, ConfigError, ManagerError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def status(self) -> dict:
        cfg_ok, cfg_err = True, None
        interval = None
        dry_run = None
        busy = False
        # Never wait on (or rebuild components under) a running sync: that
        # would swap in a Syncer whose State predates the pass's writes.
        if self._lock.acquire(blocking=False):
            try:
                try:
                    cfg, syncer = self._components()
                    interval = cfg.runtime.poll_interval_seconds
                except ConfigError as exc:
                    cfg_ok, cfg_err = False, str(exc)
                    try:
                        syncer = self._lsp_syncer()
                    except ConfigError:
                        syncer = None
                if syncer is not None:
                    dry_run = syncer.cfg.runtime.dry_run
                self._created_count = len(syncer.state.tool_created()) if syncer else 0
            finally:
                self._lock.release()
        else:
            # Busy: judge the saved config without building anything. The
            # state isn't read (a pass may be changing it); the count from the
            # last status call stands.
            busy = True
            try:
                raw = self.store.load()
                cfg = parse_config(raw)
                interval, dry_run = cfg.runtime.poll_interval_seconds, cfg.runtime.dry_run
            except ConfigError as exc:
                cfg_ok, cfg_err = False, str(exc)
                try:
                    dry_run = parse_lsp_settings(self.store.load()).runtime.dry_run
                except ConfigError:
                    pass
        created_count = self._created_count
        return {
            "config_ok": cfg_ok,
            "config_error": cfg_err,
            "poll_interval_seconds": interval,
            "dry_run": dry_run,
            # DRY_RUN env (headless testing) overrides the UI toggle.
            "dry_run_locked": os.environ.get("DRY_RUN") is not None,
            "created_count": created_count,
            "loop_running": bool(self._thread and self._thread.is_alive()),
            "busy": busy,
            "last_run": self.last_run,
            "last_error": self.last_error,
            "last_clean": (datetime.fromtimestamp(self.last_clean, timezone.utc).isoformat()
                           if self.last_clean else None),
            "sync_ok": self.sync_health()[0],
            "sync_health": self.sync_health()[1],
            "state_error": self._state_error,
            "config_error_restored": self.store.load_error,
        }

    def health(self) -> tuple[bool, str]:
        """For the container health check: is the loop alive and not stuck?"""
        if not (self._thread and self._thread.is_alive()):
            return False, "sync loop is not running"
        age = time.monotonic() - self._heartbeat
        if age > self._interval + _STUCK_SECONDS:
            return False, f"no sync activity for {int(age)}s"
        return True, "ok"

    # -- background loop ----------------------------------------------------

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._heartbeat = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name="sync-loop", daemon=True)
        self._thread.start()
        if self._on_unhealthy is not None:
            threading.Thread(target=self._watchdog, name="sync-watchdog", daemon=True).start()

    def _watchdog(self) -> None:
        """Restart the process (via on_unhealthy) once the loop has died or
        stopped making progress. State is saved after every LSP write, so
        abandoning a stuck pass loses nothing."""
        while not self._stop.wait(_WATCHDOG_SECONDS):
            ok, why = self.health()
            if not ok:
                log.error("Sync loop unhealthy (%s); restarting", why)
                self._alerter.send(self._webhook(), f"⚠️ LiveScheduleSheets is restarting: {why}")
                self._on_unhealthy(why)
                return

    def stop(self, timeout: Optional[float] = None) -> None:
        """Ask the loop to stop and wait (up to `timeout`) for a pass in
        progress to finish its current LSP write and save state."""
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("Sync pass still running at shutdown")

    def _loop(self) -> None:
        log.info("Background sync loop started")
        while not self._stop.is_set():
            interval = 900
            self._heartbeat = time.monotonic()
            try:
                with self._lock:
                    cfg, syncer = self._components()
                    interval = self._interval = cfg.runtime.poll_interval_seconds
                    summary = syncer.run_once()
                self._finished(summary, "scheduled")
            except ConfigError as exc:
                self.last_error = str(exc)
                log.warning("Config not ready: %s", exc)
                self._pass_result(False, self.last_error, None)
                # Settings, the key file or the state file's lock (another
                # copy still stopping during a redeploy): no network involved,
                # so look again soon rather than a whole interval later.
                interval = _CONFIG_RETRY_SECONDS
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Unhandled error during sync pass")
                self._pass_result(False, self.last_error, None)

            self._sleep(interval)
        log.info("Background sync loop stopped")

    def settings_saved(self) -> None:
        """The settings were saved: a new poll interval applies to the wait
        already under way, not only from the next pass on."""
        self._wake.set()

    def _sleep(self, interval: int) -> None:
        """Wait `interval` seconds from now, or until shutdown. Re-reads the
        saved interval whenever the settings are saved."""
        self._heartbeat = began = time.monotonic()
        # A save during the pass leaves _wake set, so it is read at once.
        deadline = began + max(60, interval)
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._wake.wait(remaining):
                return
            self._wake.clear()
            try:
                interval = parse_lsp_settings(self.store.load()).runtime.poll_interval_seconds
            except (ConfigError, OSError):
                continue  # keep the wait as it was
            self._interval = interval
            deadline = began + max(60, interval)


def _service_account_email(cfg) -> str:
    try:
        with open(cfg.google_credentials_file, encoding="utf-8") as fh:
            return json.load(fh).get("client_email") or "the service account"
    except (OSError, ValueError):
        return "the service account"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
