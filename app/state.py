"""Local JSON record of the sheet events this tool has put in LSP.

Each sheet event the tool schedules gets a *record* (keyed by a random id)
holding where it came from in the sheet (tab, column, name, date, occurrence),
the values last applied from the sheet, and one *booking* per LSP channel it
is on. A booking keeps a snapshot of the LSP event (Name/Start/End/ChannelId)
exactly as LSP reported it after the tool last wrote it; if LSP later differs
from that snapshot, someone changed it by hand and the record is *locked* so
the tool never touches it again (until unlocked in the UI).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: str):
        self.path = path
        self.events: dict[str, dict] = {}      # record id -> record
        # Set when the file was unreadable JSON and had to be set aside.
        self.load_error: Optional[str] = None
        self._load()

    def _load(self) -> None:
        """Read the state file. A missing file is a fresh start. A file that
        can't be read (permissions, I/O) raises OSError: starting empty and
        then overwriting it would lose every record. A file that isn't valid
        JSON is renamed aside (kept for inspection) and reported in
        `load_error`, and the tool starts fresh."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as fh:
            raw = fh.read()
        try:
            data = json.loads(raw.decode("utf-8"))  # UnicodeDecodeError is a ValueError
            if not isinstance(data, dict) or not isinstance(data.get("events", {}), dict):
                raise ValueError("not a state file")
        except ValueError as exc:
            aside = f"{self.path}.corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
            os.replace(self.path, aside)
            self.load_error = (f"The state file was unreadable ({exc}) and was moved to {aside}. "
                               "Tracked events start fresh: existing LSP events are matched by "
                               "name and start and treated as made by hand.")
            log.error("%s", self.load_error)
            return
        self.events = data.get("events", {})
        log.info("Loaded state: %d tracked event(s)", len(self.events))
        if data.get("created"):
            # The pre-2026-09-28 format; no longer read. Dropped on save.
            log.info("Dropping %d booking(s) in the old state format", len(data["created"]))

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
        for rid, rec in self.events.items():
            for cid, b in rec.get("bookings", {}).items():
                if b.get("by_tool") and b.get("event_id"):
                    out.append(((rid, cid), {
                        "name": b.get("name") or rec.get("lsp_name"),
                        "pcr": rec.get("pcr"), "source_tab": rec.get("tab"),
                        "channel_id": cid, "channel_name": b.get("channel_name"),
                        "event_id": b["event_id"], "created_at": b.get("created_at"),
                        "start": rec.get("start"), "end": rec.get("end"),
                        "locked": bool(rec.get("locked")),
                    }))
        return out

    def prune(self, ended_before: datetime) -> int:
        """Drop records whose event ended before `ended_before` (LSP has
        cleaned those events up too). Returns how many were dropped."""
        old = [rid for rid, rec in self.events.items()
               if (end := _parse_time(rec.get("end"))) is not None and end < ended_before]
        for rid in old:
            del self.events[rid]
        return len(old)

    def forget(self, ref: tuple) -> None:
        """Drop one booking; a record left with none is dropped too, so the
        next pass schedules that sheet event afresh."""
        rid, cid = ref
        rec = self.events.get(rid)
        if rec is None:
            return
        rec.get("bookings", {}).pop(cid, None)
        if not rec.get("bookings"):
            self.events.pop(rid, None)

    def forget_event(self, event_id: str) -> None:
        """Drop every booking of this LSP event id (tool-made or not); a record
        left with none is dropped too."""
        for rid, rec in list(self.events.items()):
            bookings = rec.get("bookings", {})
            for cid, b in list(bookings.items()):
                if b.get("event_id") == event_id:
                    del bookings[cid]
            if not bookings:
                del self.events[rid]

    def save(self) -> None:
        """Write the state atomically. Raises OSError (disk full, read-only
        volume ...) so the failure reaches the UI's status instead of LSP
        changes going unrecorded without a trace."""
        try:
            atomic_write(self.path, lambda fh: json.dump({"events": self.events}, fh, indent=2))
        except OSError:
            log.exception("Could not write state file %s", self.path)
            raise


def _parse_time(value) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def atomic_write(path: str, write) -> None:
    """Write a text file via a temp file + rename, so a crash never leaves it
    half-written; the temp file is removed if writing fails."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
