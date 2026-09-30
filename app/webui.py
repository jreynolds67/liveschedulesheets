"""Flask web UI + JSON API for configuring and operating the sync service.

Runs the background sync loop in-process. Serve with waitress:
    python -m app.webui
"""
from __future__ import annotations

import functools
import hmac
import logging
import os
import signal
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory

from .config import ConfigError, parse_config
from .manager import SyncManager
from .runtime import setup_logging
from .settings_store import ConfigConflict, GoogleKeyError, SettingsStore

log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.yaml")
SEED_PATH = os.environ.get("CONFIG_SEED", os.path.join(os.path.dirname(HERE), "config.example.yaml"))


def _auth_enabled() -> bool:
    return bool(os.environ.get("UI_USER") or os.environ.get("UI_PASSWORD"))


def _check_auth() -> bool:
    user = os.environ.get("UI_USER")
    pw = os.environ.get("UI_PASSWORD")
    if not _auth_enabled():
        return True  # auth disabled
    auth = request.authorization
    return bool(auth
                and hmac.compare_digest((auth.username or "").encode(), (user or "").encode())
                and hmac.compare_digest((auth.password or "").encode(), (pw or "").encode()))


def _cross_site() -> bool:
    """Whether a request came from another site's page (CSRF). Browsers send
    Origin on every cross-origin POST (and Sec-Fetch-Site), so a page
    elsewhere on the LAN can't drive the API; clients that send neither
    (curl, Companion, scripts) are unaffected."""
    if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
        return True
    origin = request.headers.get("Origin")
    return origin is not None and urlsplit(origin).netloc.lower() != request.host.lower()


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _check_auth():
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="LiveScheduleSheets"'},
            )
        return fn(*args, **kwargs)
    return wrapper


def create_app(manager: SyncManager, store: SettingsStore) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # bounds uploads (Google key)

    @app.before_request
    def refuse_cross_site_writes():
        if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site():
            log.warning("Refused cross-site %s %s (Origin %s)", request.method, request.path,
                        request.headers.get("Origin"))
            return Response("Cross-site request refused", 403)
        return None

    @app.get("/")
    @require_auth
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/api/config")
    @require_auth
    def get_config():
        return jsonify(store.load_safe())

    @app.post("/api/config")
    @require_auth
    def post_config():
        """Save the UI's settings. A body carrying the `_version` it was
        loaded with gets 409 if the saved settings changed since (another
        window), instead of overwriting them. The dry-run switch is not
        saved here: see /api/dry-run."""
        incoming = request.get_json(force=True, silent=True) or {}
        try:
            saved = store.save_from_ui(incoming)
        except ConfigConflict as exc:
            return jsonify({"saved": False, "conflict": True, "error": str(exc)}), 409
        manager.settings_saved()
        # Report whether the saved config is fully valid (loop tolerates invalid).
        result = {"saved": True, "valid": True, "error": None, "version": saved["version"],
                  "password_cleared": saved["password_cleared"]}
        try:
            parse_config(saved["raw"])
        except ConfigError as exc:
            result["valid"] = False
            result["error"] = str(exc)
        return jsonify(result)

    @app.post("/api/dry-run")
    @require_auth
    def set_dry_run():
        """The dry-run safety switch. Body: {dry_run: bool}"""
        body = request.get_json(force=True, silent=True) or {}
        if not isinstance(body.get("dry_run"), bool):
            return jsonify({"ok": False, "error": "dry_run (true/false) is required"})
        store.set_live(not body["dry_run"])
        return jsonify({"ok": True, **manager.status()})

    @app.get("/api/google-key")
    @require_auth
    def google_key():
        return jsonify({"ok": True, **store.key_info()})

    @app.post("/api/google-key")
    @require_auth
    def upload_google_key():
        """Body: the service-account JSON file's contents."""
        try:
            return jsonify({"ok": True, **store.save_google_key(request.get_data())})
        except GoogleKeyError as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @app.get("/api/status")
    @require_auth
    def status():
        return jsonify({**manager.status(), "ui_auth": _auth_enabled()})

    @app.get("/healthz")
    def healthz():
        """Container health check (no auth; says only ok / why not)."""
        ok, why = manager.health()
        return Response(why, 200 if ok else 503, mimetype="text/plain")

    @app.get("/api/channels")
    @require_auth
    def channels():
        try:
            return jsonify({"ok": True, "channels": manager.channels()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.get("/api/event-name-channels")
    @require_auth
    def event_name_channels():
        """Whether each PCR channel's events carry the event-name variable."""
        try:
            return jsonify({"ok": True, **manager.event_name_channels()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.get("/api/lsp-event/<event_id>")
    @require_auth
    def lsp_event(event_id):
        """One LSP event's raw JSON (variables and all), for troubleshooting."""
        try:
            return jsonify({"ok": True, "event": manager.lsp_event(event_id)})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.get("/api/tabs")
    @require_auth
    def tabs():
        # ?spreadsheet=<link or ID> reads a just-pasted sheet before it is saved.
        try:
            return jsonify({"ok": True, **manager.tabs(request.args.get("spreadsheet"))})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/sheet/inspect")
    @require_auth
    def sheet_inspect():
        """Grid preview + row detection for one tab (row-mapping screen).
        Body: {tab, spreadsheet?, rows?, refresh?}"""
        body = request.get_json(force=True, silent=True) or {}
        tab = body.get("tab")
        if not tab:
            return jsonify({"ok": False, "error": "tab is required"})
        try:
            result = manager.inspect_tab(
                tab,
                spreadsheet=body.get("spreadsheet"),
                rows=body.get("rows"),
                refresh=bool(body.get("refresh")),
            )
            return jsonify({"ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/test")
    @require_auth
    def test():
        return jsonify(manager.test_connection())

    @app.get("/api/preview")
    @require_auth
    def preview():
        try:
            return jsonify({"ok": True, "items": manager.preview()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/run")
    @require_auth
    def run():
        try:
            return jsonify({"ok": True, "summary": manager.run_now()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/send-event")
    @require_auth
    def send_event():
        """Create one previewed event in LSP now, even in dry run.
        Body: {source_tab, event_date, event_name, occurrence?}"""
        body = request.get_json(force=True, silent=True) or {}
        if not body.get("source_tab") or not body.get("event_name"):
            return jsonify({"ok": False, "error": "source_tab and event_name are required"})
        try:
            summary = manager.send_event(body["source_tab"], body.get("event_date") or "",
                                         body["event_name"], int(body.get("occurrence") or 0))
            return jsonify({"ok": True, "summary": summary})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/unlock")
    @require_auth
    def unlock():
        """Let the tool manage an event locked by a hand edit in LSP again.
        Body: {record_id}"""
        body = request.get_json(force=True, silent=True) or {}
        if not body.get("record_id"):
            return jsonify({"ok": False, "error": "record_id is required"})
        try:
            return jsonify({"ok": True, **manager.unlock_event(body["record_id"])})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.get("/api/scheduled")
    @require_auth
    def scheduled():
        # ?past_days=N also includes events that ended in the last N days.
        try:
            past_days = int(request.args.get("past_days", 0))
            return jsonify({"ok": True, **manager.scheduled_events(past_days)})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/delete-created")
    @require_auth
    def delete_created():
        try:
            return jsonify({"ok": True, "summary": manager.delete_created()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/delete-events")
    @require_auth
    def delete_events():
        """Delete one event from LSP (all its channel bookings).
        Body: {event_ids: [...]}"""
        body = request.get_json(force=True, silent=True) or {}
        ids = [i for i in (body.get("event_ids") or []) if isinstance(i, str) and i]
        if not ids:
            return jsonify({"ok": False, "error": "event_ids is required"})
        try:
            return jsonify({"ok": True, "summary": manager.delete_events(ids)})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    return app


# How long shutdown waits for a sync pass to reach a safe stopping point.
# Keep it under the container's stop_grace_period (docker-compose.yml).
SHUTDOWN_WAIT_SECONDS = 45


# Exit status: non-zero after the watchdog found the sync loop dead or stuck.
_exit_code = 0


def _exit_on_sigterm(signum, _frame):
    # Docker stops the container with SIGTERM, which by default kills Python
    # without running `finally` blocks. Turn it into SystemExit so serve()
    # returns and main() stops the sync loop cleanly.
    log.info("Signal %s; shutting down", signum)
    raise SystemExit(_exit_code)


def _restart(_why: str) -> None:
    """Shut down like a SIGTERM so Docker's restart policy starts a fresh
    process (it doesn't act on an unhealthy container)."""
    global _exit_code
    _exit_code = 1
    signal.raise_signal(signal.SIGTERM)


def main() -> int:
    setup_logging()
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    store = SettingsStore(CONFIG_PATH, SEED_PATH)
    manager = SyncManager(store, on_unhealthy=_restart)
    manager.start_loop()

    app = create_app(manager, store)
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    log.info("Web UI on http://%s:%d", host, port)
    if not _auth_enabled():
        log.warning("The web UI has no password: anyone who can reach it can change settings "
                    "and delete LSP events. Set UI_USER / UI_PASSWORD.")

    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port)
    finally:
        manager.stop(timeout=SHUTDOWN_WAIT_SECONDS)
    return _exit_code


if __name__ == "__main__":
    raise SystemExit(main())
