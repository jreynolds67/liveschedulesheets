"""Read/write the live operational config (YAML on the writable /data volume).

Seeds itself from the bundled example on first run so a fresh container starts
with a sensible, editable config.
"""
from __future__ import annotations

import copy
import logging
import os
import shutil
import threading

from .config import load_raw, save_raw

log = logging.getLogger(__name__)

# Keys under lsp that are secret and must never be sent to the browser as-is.
_SECRET_LSP_KEYS = ("password",)


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
