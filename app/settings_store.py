"""Read/write the live operational config (YAML on the writable /data volume).

Seeds itself from the bundled example on first run so a fresh container starts
with a sensible, editable config.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import shutil
import threading
from typing import Callable
from urllib.parse import urlsplit

from .config import effective_password, load_raw, normalize_url, save_raw

log = logging.getLogger(__name__)

# Google service-account key uploaded from the web UI, stored next to config.yaml.
GOOGLE_KEY_FILENAME = "google-service-account.json"
_MAX_KEY_BYTES = 64 * 1024


# Config sections the web UI edits (and sends back whole on every save).
UI_SECTIONS = ("sheet", "date_parsing", "scheduling", "pcr_channel_map",
               "lsp", "runtime", "tab_overrides", "event_overrides")


class GoogleKeyError(ValueError):
    """An uploaded Google key was rejected."""


class ConfigConflict(Exception):
    """The UI's copy of the config is older than the saved one."""


def config_version(raw: dict) -> str:
    """A fingerprint of the settings a UI save would overwrite. Leaves out
    what is saved on its own (the dry-run switch, the password, the Google
    key), so changing those in one window doesn't invalidate another's."""
    view = {k: copy.deepcopy(raw.get(k)) for k in UI_SECTIONS}
    for k in ("password", "password_for"):
        (view.get("lsp") or {}).pop(k, None)
    (view.get("runtime") or {}).pop("live", None)
    blob = json.dumps(view, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:16]


def _placeholder_url(url: str) -> bool:
    """No URL yet, or the example config's reserved-domain placeholder."""
    host = (urlsplit(url).hostname or "").lower()
    return not url or host.endswith((".example.com", ".example", ".invalid"))


class SettingsStore:
    def __init__(self, path: str, seed_path: str):
        self.path = path
        self.seed_path = seed_path
        # Re-entrant: update() holds it across load + save.
        self._lock = threading.RLock()
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

    def update(self, change: Callable[[dict], None]) -> dict:
        """Load, apply `change` (which edits the dict in place), and save, as
        one step, so two saves at once can't drop each other's changes."""
        with self._lock:
            raw = self.load()
            change(raw)
            self.save(raw)
            return raw

    def set_live(self, live: bool) -> None:
        """The dry-run switch, saved on its own: a general save never touches
        it, so a page left open with an old setting can't flip it back."""
        self.update(lambda raw: raw.setdefault("runtime", {}).update(live=bool(live)))
        log.warning("Dry run turned %s from the web UI", "OFF (live)" if live else "ON")

    def load_safe(self) -> dict:
        """Config for the browser: password removed, replaced with a 'set' flag."""
        raw = copy.deepcopy(self.load())
        version = config_version(raw)
        lsp = raw.get("lsp") or {}
        lsp["password_set"] = bool(effective_password(lsp))
        lsp.pop("password", None)
        lsp.pop("password_for", None)
        raw["lsp"] = lsp
        raw["_version"] = version
        # never expose a stored key path beyond a boolean
        google = raw.get("google") or {}
        raw["google_credentials_set"] = bool(
            google.get("credentials_file") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        )
        raw.pop("google", None)
        return raw

    # -- Google service-account key -------------------------------------------

    @property
    def google_key_path(self) -> str:
        return os.path.join(os.path.dirname(self.path) or ".", GOOGLE_KEY_FILENAME)

    def save_google_key(self, data: bytes) -> dict:
        """Validate and store an uploaded service-account key (owner-only
        permissions), and point the config at it. Returns key_info()."""
        if len(data) > _MAX_KEY_BYTES:
            raise GoogleKeyError("That file is too large to be a service-account key")
        try:
            key = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GoogleKeyError("That file isn't valid JSON") from exc
        if not isinstance(key, dict) or key.get("type") != "service_account":
            raise GoogleKeyError("That isn't a Google service-account key (expected \"type\": \"service_account\")")
        missing = [f for f in ("client_email", "private_key", "token_uri") if not key.get(f)]
        if missing:
            raise GoogleKeyError(f"The key is missing {', '.join(missing)}")

        path = self.google_key_path
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

        self.update(lambda raw: raw.update(
            google={**(raw.get("google") or {}), "credentials_file": path}))
        log.info("Stored uploaded Google service-account key for %s", key["client_email"])
        return self.key_info()

    def key_info(self) -> dict:
        """Which Google key is in use and its service-account email (never the key)."""
        raw = self.load()
        path = ((raw.get("google") or {}).get("credentials_file")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
        info = {"installed": False, "uploaded": path == self.google_key_path,
                "client_email": None, "error": None}
        if not path:
            info["error"] = "No key set"
            return info
        if not os.path.isfile(path):
            info["error"] = "No key file found"
            return info
        try:
            with open(path, encoding="utf-8") as fh:
                key = json.load(fh)
            info["client_email"] = key.get("client_email")
            info["installed"] = bool(info["client_email"])
        except (OSError, ValueError) as exc:
            info["error"] = f"Key file unreadable: {exc}"
        return info

    def save_from_ui(self, incoming: dict) -> dict:
        """Merge a UI payload onto the stored config and save it, atomically.

        If the payload carries the `_version` it was loaded with and the
        stored config has changed since (another browser window saved),
        raises ConfigConflict instead of overwriting that change. Returns
        {"raw", "version", "password_cleared"}.
        """
        out = {}

        def change(raw):
            expected = incoming.get("_version")
            if expected and expected != config_version(raw):
                raise ConfigConflict("The settings were changed in another browser window "
                                     "since this page loaded them")
            had_password = bool(effective_password(raw.get("lsp") or {}))
            merged = self.merge_from_ui(incoming, raw)
            raw.clear()
            raw.update(merged)
            out["password_cleared"] = had_password and not effective_password(raw.get("lsp") or {})

        raw = self.update(change)
        return {"raw": raw, "version": config_version(raw), **out}

    def merge_from_ui(self, incoming: dict, current: dict) -> dict:
        """A UI payload merged onto `current`.

        - The password is kept unless a new non-empty one was supplied.
        - Changing the LSP server URL without re-entering the password
          drops it (and stops LSP_PASSWORD env following it), so whoever
          can reach this UI can't point it at their own server to collect
          the login. The first real URL replacing the example placeholder
          is exempt.
        - The dry-run switch (runtime.live) is never taken from a general
          save; see set_live().
        """
        merged = copy.deepcopy(current)
        for section in UI_SECTIONS:
            if section in incoming and incoming[section] is not None:
                merged[section] = copy.deepcopy(incoming[section])

        old_lsp = current.get("lsp") or {}
        lsp = merged.setdefault("lsp", {})
        lsp.pop("password_set", None)
        old_url, new_url = normalize_url(old_lsp.get("base_url")), normalize_url(lsp.get("base_url"))
        new_pw = (incoming.get("lsp") or {}).get("password")
        if new_pw:
            lsp["password_for"] = new_url
        else:
            lsp.pop("password", None)
            lsp.pop("password_for", None)
            if old_lsp.get("password"):
                lsp["password"] = old_lsp["password"]
            if old_lsp.get("password_for"):
                lsp["password_for"] = old_lsp["password_for"]
            if (new_url != old_url and not _placeholder_url(old_url)
                    and effective_password(old_lsp)):
                lsp.pop("password", None)
                lsp["password_for"] = normalize_url(old_lsp.get("password_for")) or old_url
                log.warning("LSP server URL changed from %s to %s without the password being "
                            "entered again; the password is cleared", old_url, new_url)

        runtime = merged.setdefault("runtime", {})
        if "live" in (current.get("runtime") or {}):
            runtime["live"] = current["runtime"]["live"]
        else:
            runtime.pop("live", None)
        return merged
