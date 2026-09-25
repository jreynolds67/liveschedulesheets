"""Read/write the live operational config (YAML on the writable /data volume).

Seeds itself from the bundled example on first run so a fresh container starts
with a sensible, editable config.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading

from .config import load_raw, save_raw

log = logging.getLogger(__name__)

# Keys under lsp that are secret and must never be sent to the browser as-is.
_SECRET_LSP_KEYS = ("password",)

# Google credentials uploaded from the web UI, stored next to config.yaml: a
# service-account key, or a user sign-in ("authorized_user") from
# tools/google_login.py. (Filename kept for existing deployments.)
GOOGLE_KEY_FILENAME = "google-service-account.json"
_MAX_KEY_BYTES = 64 * 1024


class GoogleKeyError(ValueError):
    """An uploaded Google key was rejected."""


class SettingsStore:
    def __init__(self, path: str, seed_path: str):
        self.path = path
        self.seed_path = seed_path
        self._lock = threading.Lock()
        self._ensure()

    def _ensure(self) -> None:
        if os.path.exists(self.path):
            return
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.seed_path):
            shutil.copyfile(self.seed_path, self.path)
            log.info("Seeded config at %s from %s", self.path, self.seed_path)
        else:
            save_raw(self.path, {})
            log.warning("No seed found at %s; created empty config %s", self.seed_path, self.path)

    def load(self) -> dict:
        with self._lock:
            return load_raw(self.path)

    def save(self, raw: dict) -> None:
        with self._lock:
            save_raw(self.path, raw)

    def load_safe(self) -> dict:
        """Config for the browser: password removed, replaced with a 'set' flag."""
        raw = copy.deepcopy(self.load())
        lsp = raw.get("lsp") or {}
        pw = lsp.get("password")
        env_pw = bool(os.environ.get("LSP_PASSWORD"))
        lsp.pop("password", None)
        lsp["password_set"] = bool(pw) or env_pw
        raw["lsp"] = lsp
        # never expose a stored key path beyond a boolean
        google = raw.get("google") or {}
        raw["google_credentials_set"] = bool(
            google.get("credentials_file") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        )
        raw.pop("google", None)
        return raw

    # -- Google credentials (service-account key or user sign-in) --------------

    @property
    def google_key_path(self) -> str:
        return os.path.join(os.path.dirname(self.path) or ".", GOOGLE_KEY_FILENAME)

    def save_google_key(self, data: bytes) -> dict:
        """Validate and store uploaded Google credentials (owner-only
        permissions), and point the config at them. Returns key_info()."""
        if len(data) > _MAX_KEY_BYTES:
            raise GoogleKeyError("That file is too large to be a Google credentials file")
        try:
            key = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GoogleKeyError("That file isn't valid JSON") from exc
        kind = key.get("type") if isinstance(key, dict) else None
        if kind == "service_account":
            required = ("client_email", "private_key", "token_uri")
        elif kind == "authorized_user":
            required = ("client_id", "client_secret", "refresh_token")
        elif isinstance(key, dict) and ("installed" in key or "web" in key):
            raise GoogleKeyError("That's an OAuth client file, not a sign-in -- run "
                                 "tools/google_login.py with it and upload the file it writes")
        else:
            raise GoogleKeyError("That isn't a Google service-account key or user sign-in "
                                 "(expected \"type\": \"service_account\" or \"authorized_user\")")
        missing = [f for f in required if not key.get(f)]
        if missing:
            raise GoogleKeyError(f"The file is missing {', '.join(missing)}")

        path = self.google_key_path
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

        raw = self.load()
        raw["google"] = {**(raw.get("google") or {}), "credentials_file": path}
        self.save(raw)
        log.info("Stored uploaded Google %s credentials for %s", kind,
                 key.get("client_email") or key.get("account") or "an unnamed account")
        return self.key_info()

    def key_info(self) -> dict:
        """Which Google credentials are in use and their account email (never
        the secret). kind is "service_account" or "authorized_user"."""
        raw = self.load()
        path = ((raw.get("google") or {}).get("credentials_file")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
        info = {"installed": False, "uploaded": path == self.google_key_path,
                "client_email": None, "kind": None, "error": None}
        if not path:
            info["error"] = "No key set"
            return info
        if not os.path.isfile(path):
            info["error"] = "No key file found"
            return info
        try:
            with open(path, encoding="utf-8") as fh:
                key = json.load(fh)
            info["kind"] = key.get("type")
            info["client_email"] = key.get("client_email") or key.get("account")
            info["installed"] = (bool(key.get("client_email")) if info["kind"] == "service_account"
                                 else bool(key.get("refresh_token")))
        except (OSError, ValueError) as exc:
            info["error"] = f"Key file unreadable: {exc}"
        return info

    def merge_from_ui(self, incoming: dict) -> dict:
        """Merge a UI payload onto the stored config, preserving the password
        unless a new non-empty one was supplied."""
        current = self.load()
        merged = copy.deepcopy(current)

        for section in ("sheet", "date_parsing", "scheduling", "pcr_channel_map",
                        "lsp", "runtime", "tab_overrides", "event_overrides"):
            if section in incoming and incoming[section] is not None:
                merged[section] = incoming[section]

        # Preserve existing password if the UI sent an empty/blank one.
        incoming_lsp = incoming.get("lsp") or {}
        new_pw = incoming_lsp.get("password")
        if not new_pw:
            old_pw = (current.get("lsp") or {}).get("password")
            if old_pw:
                merged.setdefault("lsp", {})["password"] = old_pw
            else:
                merged.get("lsp", {}).pop("password", None)
        merged.get("lsp", {}).pop("password_set", None)
        return merged
