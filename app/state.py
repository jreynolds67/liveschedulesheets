"""Local JSON record of the sheet events this tool has put in LSP.

Each sheet event the tool schedules gets a *record* (keyed by a random id)
holding where it came from in the sheet (tab, column, name, date, occurrence),
the values last applied from the sheet, and one *booking* per LSP channel it
is on. A booking keeps a snapshot of the LSP event (Name/Start/End/ChannelId)
exactly as LSP reported it after the tool last wrote it; if LSP later differs
from that snapshot, someone changed it by hand and the record is *locked* so
the tool never touches it again (until unlocked in the UI).

`created` holds the pre-records format (one hash key per booking). It is only
read to adopt those bookings into records, and to let cleanup delete them.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: str):
        self.path = path
        self.created: dict[str, Any] = {}      # legacy booking cache
        self.events: dict[str, dict] = {}      # record id -> record
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.created = data.get("created", {})
            self.events = data.get("events", {})
            log.info("Loaded state: %d tracked event(s), %d legacy booking(s)",
                     len(self.events), len(self.created))
        except (OSError, ValueError):
            log.exception("Could not read state file %s; starting fresh", self.path)
            self.created, self.events = {}, {}

    # -- legacy bookings ------------------------------------------------------

    def has(self, key: str) -> bool:
        return key in self.created

    def mark(self, key: str, info: dict) -> None:
        self.created[key] = info

    def unmark(self, key: str) -> None:
        self.created.pop(key, None)

    # -- records --------------------------------------------------------------

    def new_record(self, fields: dict) -> str:
        rid = uuid.uuid4().hex[:12]
        self.events[rid] = {**fields, "locked": False, "lock_reason": "", "bookings": {}}
        return rid

    def record(self, rid: str) -> Optional[dict]:
        return self.events.get(rid)

    def lock(self, rid: str, reason: str, when: str) -> None:
        rec = self.events.get(rid)
        if rec is not None and not rec.get("locked"):
            rec.update(locked=True, lock_reason=reason, locked_at=when)

    # -- tool-created bookings (scheduled view, cleanup) -----------------------

    def tool_created(self) -> list[tuple[tuple, dict]]:
        """(ref, info) for every booking THIS tool created in LSP.

        Excludes bookings that were already in LSP (made by hand), which we
        must never delete. `ref` is passed back to `forget`.
        """
        out = []
        for key, info in self.created.items():
            if not isinstance(info, dict):
                continue
            if info.get("existed") and not info.get("created_by_tool"):
                continue
            if info.get("event_id"):
                out.append((("legacy", key), info))
        for rid, rec in self.events.items():
            for cid, b in rec.get("bookings", {}).items():
                if b.get("by_tool") and b.get("event_id"):
                    out.append((("record", rid, cid), {
                        "name": b.get("name") or rec.get("lsp_name"),
                        "pcr": rec.get("pcr"), "source_tab": rec.get("tab"),
                        "channel_id": cid, "channel_name": b.get("channel_name"),
                        "event_id": b["event_id"], "created_at": b.get("created_at"),
                        "locked": bool(rec.get("locked")),
                    }))
        return out

    def forget(self, ref: tuple) -> None:
        """Drop one booking; a record left with none is dropped too, so the
        next pass schedules that sheet event afresh."""
        if ref[0] == "legacy":
            self.created.pop(ref[1], None)
            return
        _, rid, cid = ref
        rec = self.events.get(rid)
        if rec is None:
            return
        rec.get("bookings", {}).pop(cid, None)
        if not rec.get("bookings"):
            self.events.pop(rid, None)

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"created": self.created, "events": self.events}, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write state file %s", self.path)
