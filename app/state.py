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
import glob
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Optional

try:
    import fcntl
except ImportError:  # not on Windows; the container is Linux
    fcntl = None

log = logging.getLogger(__name__)


# Daily copies kept of the state file and config.yaml (in backups/ next to them).
BACKUP_DAYS = 14


class State:
    def __init__(self, path: str, exclusive: bool = False):
        """`exclusive`: hold a lock on the state file for this object's life,
        so a second copy of the tool using the same /data (a test container,
        RUN_ONCE alongside the service) is refused instead of both writing
        to LSP and the file at once. Raises OSError if another holds it."""
        self.path = path
        self._lock_fh = _lock(path) if exclusive else None
        self.events: dict[str, dict] = {}      # record id -> record
        # spreadsheet id -> tabs the tool has read there; a tab not listed
        # (added or renamed since) isn't read until it's turned on in the UI.
        self.tabs_seen: dict[str, list[str]] = {}
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
            data = _parse_state(raw)
        except ValueError as exc:
            aside = f"{self.path}.corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
            os.replace(self.path, aside)
            restored = _newest_backup(self.path, _parse_state)
            if restored is None:
                self.load_error = (
                    f"The state file was unreadable ({exc}) and was moved to {aside}. Tracked "
                    "events start fresh: existing LSP events are matched by name and start and "
                    "treated as made by hand.")
                log.error("%s", self.load_error)
                return
            data, backup = restored
            self.load_error = (
                f"The state file was unreadable ({exc}) and was moved to {aside}; restored "
                f"{os.path.basename(backup)}. Events scheduled after that backup are matched "
                "by name and start and treated as made by hand.")
            log.error("%s", self.load_error)
        self.events = data.get("events", {})
        self.tabs_seen = data.get("tabs_seen") or {}
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

    def known_tabs(self, spreadsheet_id: str) -> Optional[set]:
        """Tabs the tool has read on this sheet; None if it never has (a
        new sheet: every visible tab is read and remembered)."""
        seen = self.tabs_seen.get(spreadsheet_id)
        return None if seen is None else set(seen)

    def note_tabs(self, spreadsheet_id: str, tabs) -> None:
        self.tabs_seen[spreadsheet_id] = sorted(set(self.tabs_seen.get(spreadsheet_id, [])) | set(tabs))

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
                        "start": (s.isoformat() if (s := record_start(rec)) else rec.get("start")),
                        "end": rec.get("end"),
                        "locked": bool(rec.get("locked")),
                        "test": bool(rec.get("test")),
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
            atomic_write(self.path, lambda fh: json.dump({"events": self.events, "tabs_seen": self.tabs_seen},
                                               fh, indent=2))
        except OSError:
            log.exception("Could not write state file %s", self.path)
            raise
        keep_daily_backup(self.path)


def record_start(rec: dict) -> Optional[datetime]:
    """When a record's event starts in LSP: the earliest of the record's
    start and the start last written to each of its channels. They differ
    after a pass whose update reached some channels but not others; a
    channel that starts sooner counts, so a recording is never changed."""
    starts = [_parse_time(rec.get("start"))]
    # Not a channel LSP no longer lists: its event went with it, and its
    # old time would make the event look started early.
    starts += [_parse_time((b.get("written") or {}).get("Start"))
               for b in (rec.get("bookings") or {}).values()
               if b.get("by_tool") and not b.get("gone_since")]
    starts = [s for s in starts if s is not None]
    return min(starts) if starts else None


def _parse_time(value) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def atomic_write(path: str, write) -> None:
    """Write a text file via a temp file + rename, so a crash never leaves it
    half-written; the temp file is removed if writing fails. Flushed to disk
    before and after the rename, so a power cut can't leave an empty file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            write(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):  # the rename itself (not on every OS)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _backup_dir(path: str) -> str:
    return os.path.join(os.path.dirname(path) or ".", "backups")


def keep_daily_backup(path: str, keep: int = BACKUP_DAYS) -> None:
    """Copy `path` into backups/ once a day (name.YYYYMMDD), keeping the
    newest `keep`. Never raises: a backup must not fail what it backs up."""
    try:
        directory = _backup_dir(path)
        base = os.path.basename(path)
        dst = os.path.join(directory, f"{base}.{datetime.now(timezone.utc):%Y%m%d}")
        if os.path.exists(dst):
            return
        os.makedirs(directory, exist_ok=True)
        shutil.copy2(path, dst + ".tmp")  # keeps owner-only permissions (passwords)
        os.replace(dst + ".tmp", dst)
        for old in _backups(path)[keep:]:
            os.unlink(old)
    except OSError as exc:
        log.warning("Could not back up %s: %s", path, exc)


def _backups(path: str) -> list[str]:
    """Daily backups of `path`, newest first."""
    base = glob.escape(os.path.basename(path))
    found = glob.glob(os.path.join(glob.escape(_backup_dir(path)), base + ".[0-9]*"))
    return sorted((f for f in found if not f.endswith(".tmp")), reverse=True)


def _newest_backup(path: str, parse) -> Optional[tuple]:
    """(parse(contents), backup path) for the newest backup of `path` that
    `parse` accepts (it raises ValueError for one it doesn't), or None."""
    for candidate in _backups(path):
        try:
            with open(candidate, "rb") as fh:
                return parse(fh.read()), candidate
        except (OSError, ValueError):
            continue
    return None


def _parse_state(raw: bytes) -> dict:
    data = json.loads(raw.decode("utf-8"))  # UnicodeDecodeError is a ValueError
    if not isinstance(data, dict) or not isinstance(data.get("events", {}), dict):
        raise ValueError("not a state file")
    return data


def _lock(path: str):
    """An open, exclusively locked `<path>.lock` (held until closed or the
    process ends); raises OSError if another process holds it."""
    if fcntl is None:
        return None
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path + ".lock", "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise OSError(f"Another copy of LiveScheduleSheets is using {os.path.dirname(path) or '.'} "
                      "(its state file is locked). Run only one copy against the same LSP; stop "
                      "the other one first") from None
    except OSError as exc:  # the volume can't lock files at all: carry on without
        log.warning("Can't lock %s.lock (%s); not guarding against a second copy", path, exc)
    return fh
