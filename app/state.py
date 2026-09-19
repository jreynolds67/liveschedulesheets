"""Local JSON cache of events we've already created (reduces API churn).

Live Schedule Pro remains the source of truth for de-duplication; this file is
a best-effort accelerator that survives restarts when mounted on a volume.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: str):
        self.path = path
        self.created: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.created = data.get("created", {})
            log.info("Loaded state: %d previously created event(s)", len(self.created))
        except (OSError, ValueError):
            log.exception("Could not read state file %s; starting fresh", self.path)
            self.created = {}

    def has(self, key: str) -> bool:
        return key in self.created

    def mark(self, key: str, info: dict) -> None:
        self.created[key] = info

    def unmark(self, key: str) -> None:
        self.created.pop(key, None)

    def tool_created(self) -> list[tuple[str, dict]]:
        """(key, info) pairs for events THIS tool actually created in LSP.

        Excludes entries recorded only because they already existed in LSP
        (marked `existed`), which we must never delete.
        """
        out = []
        for key, info in self.created.items():
            if not isinstance(info, dict):
                continue
            if info.get("existed") and not info.get("created_by_tool"):
                continue
            if info.get("event_id"):
                out.append((key, info))
        return out

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"created": self.created}, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write state file %s", self.path)
