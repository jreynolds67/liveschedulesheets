"""Flask web UI + JSON API for configuring and operating the sync service.

Runs the background sync loop in-process. Serve with waitress:
    python -m app.webui
"""
from __future__ import annotations

import functools
import logging
import os

from flask import Flask, Response, jsonify, request, send_from_directory

from .config import ConfigError, parse_config
from .manager import SyncManager
from .settings_store import GoogleKeyError, SettingsStore

log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.yaml")
SEED_PATH = os.environ.get("CONFIG_SEED", os.path.join(os.path.dirname(HERE), "config.example.yaml"))


def _check_auth() -> bool:
    user = os.environ.get("UI_USER")
    pw = os.environ.get("UI_PASSWORD")
    if not user and not pw:
        return True  # auth disabled
    auth = request.authorization
    return bool(auth and auth.username == user and auth.password == pw)


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
        incoming = request.get_json(force=True, silent=True) or {}
        merged = store.merge_from_ui(incoming)
        store.save(merged)
        # Report whether the saved config is fully valid (loop tolerates invalid).
        result = {"saved": True, "valid": True, "error": None}
        try:
            parse_config(merged)
        except ConfigError as exc:
            result["valid"] = False
            result["error"] = str(exc)
        return jsonify(result)

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
        return jsonify(manager.status())

    @app.get("/api/channels")
    @require_auth
    def channels():
        try:
            return jsonify({"ok": True, "channels": manager.channels()})
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

    @app.get("/api/created")
    @require_auth
    def created():
        try:
            return jsonify({"ok": True, "events": manager.created_events()})
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

    return app


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    store = SettingsStore(CONFIG_PATH, SEED_PATH)
    manager = SyncManager(store)
    manager.start_loop()

    app = create_app(manager, store)
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    log.info("Web UI on http://%s:%d", host, port)

    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port)
    finally:
        manager.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
