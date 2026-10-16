"""Orchestrate a sync: sheet -> resolve channels -> match tracked events -> plan -> apply.

Each sheet event the tool schedules is tracked as a *record* in `State`, with
one *booking* per LSP channel. A pass keeps LSP in step with the sheet:

- a new sheet event is created on every channel its PCR maps to;
- a tracked event whose name, time, date or PCR changed in the sheet (or whose
  lead-in / safety cap changed) is updated in place, and moved between
  channels when its PCR changes;
- a tracked event that someone changed in LSP by hand (edited, deleted, or
  moved to another channel) is *locked*: the tool never changes it again until
  an engineer unlocks it in the UI;
- events the tool creates or updates get their event-name variable (the
  workflow variable named `lsp.event_name_variable`) set to the event name.

`plan()` is read-only and classifies every event; `run_once()` executes the
plan. The web UI uses `plan()` for its preview so what you see is exactly what
a real pass would do.
"""
from __future__ import annotations

import copy
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from dateutil import parser as dateparser

from .config import Config
from .lsp_client import LspClient, LspError
from .lsp_client import event_status as _status
from .models import ScheduledEvent, name_key
from .runtime import LOCAL_TZ
from .sheets import SheetError, SheetReader
from .state import State, record_start

log = logging.getLogger(__name__)

# Plan statuses (per sheet event; CREATE/UPDATE/DELETE/EXISTS also per channel)
CREATE = "create"
UPDATE = "update"
DELETE = "delete"
EXISTS = "exists"
LOCKED = "locked"
NOT_IN_SHEET = "not_in_sheet"
OUT_OF_WINDOW = "out_of_window"
NO_CHANNEL = "no_channel"
ERROR = "error"

# The LSP fields a booking snapshot keeps. A change to Name, Start or End by
# someone other than this tool locks the event; CreatedByDisplayName tells the
# tool's event from one made again by hand with the same values.
_SNAPSHOT_FIELDS = ("Name", "Start", "End", "ChannelId", "CreatedByDisplayName")

# LSP event statuses (see lsp_client.EVENT_STATUSES). Only a Waiting event is
# the tool's to change: LSP has queued or started these (or they ran), and a
# Canceled one was canceled by hand.
_ACTIVE = {"queued", "recording", "complete", "failed", "timeout"}

# LSP's clock and this machine's may differ by this much before it's worth
# a warning ("started" is judged by this machine's clock).
_CLOCK_SKEW_SECONDS = 60
# A Queued event this far from its start suggests LSP queues events early,
# which would freeze them (Queued counts as started).
_EARLY_QUEUE = timedelta(minutes=30)

# Events read by id when looking for a workflow to copy onto an event that
# has none (see Syncer._workflow_template).
_TEMPLATE_READS = 5

# A booking whose channel LSP hasn't listed for this long is forgotten.
GONE_FORGET = timedelta(days=1)

# An event this close to its start counts as started: a pass takes a while,
# and an update must never reach LSP after the recording has begun.
START_MARGIN = timedelta(minutes=2)

# How long to keep a record after its event ends, when LSP's own cleanup
# threshold (settings/general EventCleanupThresholdInDays) can't be read.
DEFAULT_CLEANUP_DAYS = 365


@dataclass
class Target:
    """One LSP channel an event goes on, and what a pass would do there."""
    channel_id: str
    channel_name: str
    status: str
    message: str = ""
    event_id: Optional[str] = None
    # For a new event: a booking to record without creating anything (a hand-
    # made LSP event already there).
    adopt: Optional[dict] = None


@dataclass
class PlanItem:
    """One sheet event (or a tracked one no longer in the sheet). Its PCR maps
    to every matching LSP channel; `targets` says what happens on each and
    `status` summarizes them."""
    event: ScheduledEvent
    lsp_name: str
    status: str
    targets: list[Target] = field(default_factory=list)
    message: str = ""
    record_id: Optional[str] = None
    locked: bool = False
    new_lock: str = ""       # set when this pass finds a hand edit in LSP
    started: bool = False    # tracked and already started: its record is frozen

    def to_dict(self) -> dict:
        return {
            "name": self.lsp_name,
            "event_name": self.event.name,
            "pcr": self.event.pcr,
            "start": self.event.start.isoformat(),
            "end": self.event.end.isoformat(),
            "status": self.status,
            "message": self.message,
            "channels": [{"id": t.channel_id, "name": t.channel_name, "status": t.status,
                          "message": t.message} for t in self.targets],
            "source_tab": self.event.source_tab,
            "source_column": self.event.source_column,
            "event_date": self.event.event_date,
            "occurrence": self.event.occurrence,
            "same_day": self.event.same_day,
            "sheet_start": self.event.sheet_start,
            "record_id": self.record_id,
            "locked": self.locked or bool(self.new_lock),
        }


class _LspView:
    """Every event on a set of channels, fetched once per pass (GetEvents:
    the full events, workflow variables and all)."""

    def __init__(self, lsp: LspClient, channel_ids):
        self.by_channel: dict[str, list[dict]] = {}
        self.by_id: dict[str, tuple[str, dict]] = {}
        self.failed: set[str] = set()
        for cid in channel_ids:
            try:
                evs = lsp.get_events_for_channel(cid)
            except LspError:
                log.exception("Could not list events for channel %s", cid)
                self.failed.add(cid)
                continue
            self.by_channel[cid] = evs
            for e in evs:
                if e.get("Id"):
                    self.by_id[e["Id"]] = (cid, e)

    def add(self, e: dict, channel_id: str) -> tuple[str, dict]:
        """Add an event read on its own (by id); returns (its channel, it)."""
        cid = e.get("ChannelId") or channel_id
        if str(cid).lower() == channel_id.lower():
            cid = channel_id
        self.by_channel.setdefault(cid, []).append(e)
        if e.get("Id"):
            self.by_id[e["Id"]] = (cid, e)
        return cid, e

    def find(self, channel_id: str, name: str, start: datetime) -> Optional[dict]:
        """An event on the channel with this name (ignoring case and spacing)
        and start minute."""
        want = name_key(name)
        for e in self.by_channel.get(channel_id, []):
            if name_key(e.get("Name") or "") == want and _same_instant(e.get("Start"), start):
                return e
        return None


class Syncer:
    def __init__(self, cfg: Config, reader: Optional[SheetReader], lsp: LspClient, state: State,
                 stop: Optional[threading.Event] = None,
                 heartbeat: Optional[Callable[[], None]] = None):
        # cfg may be an LspSettings and reader None for LSP-only use (channels,
        # scheduled view, cleanup) before the sheet side is configured.
        self.cfg = cfg
        self.reader = reader
        self.lsp = lsp
        self.state = state
        # Set on shutdown: a pass stops between LSP writes (state saved).
        self.stop = stop or threading.Event()
        # Called as a pass makes progress, so a long pass isn't taken for a stuck one.
        self.heartbeat = heartbeat or (lambda: None)
        self.sheet_errors: list[str] = []
        # Not errors, but someone should look: unreadable columns, new tabs,
        # LSP behaving unexpectedly... ({"text", "key"}; see sheets._warning).
        self.sheet_warnings: list[dict] = []
        self._logged_warnings: set[str] = set()  # logged once, not every pass

    # -- planning (read-only) ----------------------------------------------

    def plan(self) -> list[PlanItem]:
        sheet_id = self.cfg.sheet.spreadsheet_id
        known = self.state.known_tabs(sheet_id)
        events = self.reader.read_events(known_tabs=known)
        self.sheet_errors = list(getattr(self.reader, "errors", None) or [])
        self.sheet_warnings = list(getattr(self.reader, "warnings", None) or [])
        # A sheet read for the first time: every tab it has now counts as
        # known, so only tabs added (or renamed) later wait to be turned on.
        self.state.note_tabs(sheet_id, getattr(self.reader, "visible", None) or []
                             if known is None else getattr(self.reader, "read_tabs", None) or [])
        if not events and not self.state.events:
            self._log_new_warnings()
            return []

        channels = self.lsp.get_all_channels()
        by_pcr = self._resolve_channels(channels)
        names = {c["Id"]: c.get("Name") or c["Id"] for c in channels if c.get("Id")}
        wanted = {cid for found in by_pcr.values() for cid, _ in found}
        booked = {cid for rec in self.state.events.values() for cid in rec.get("bookings", {})}
        # Booked channels LSP no longer lists (deleted, or recreated with a
        # new id): not read, so an event booked there isn't stuck on "could
        # not read LSP" for good. Their bookings are kept, in case the
        # channel is only missing from this one reply.
        self._gone = {cid for cid in booked if cid not in names}
        self._note_gone(_now_iso())
        view = _LspView(self.lsp, wanted | (booked - self._gone))
        self._view = view  # _execute rebaselines snapshots from it
        self._templates: dict = {}  # channel -> workflow to copy (_workflow_template)
        # LSP event id -> the record whose tool-made booking it is, so one
        # sheet event never takes another's event for a hand-made one.
        self._owned = {b["event_id"]: rid for rid, rec in self.state.events.items()
                       for b in rec.get("bookings", {}).values()
                       if b.get("by_tool") and b.get("event_id")}
        self.heartbeat()

        now = datetime.now(timezone.utc)
        matches = self._match(events, now)
        planned_keys: set[tuple[str, str, str]] = set()
        items: list[PlanItem] = []
        for i, ev in enumerate(events):
            rid = matches.get(i)
            if rid is None:
                items.append(self._plan_new(ev, by_pcr, view, planned_keys))
            else:
                items.append(self._plan_tracked(ev, rid, by_pcr, names, view, now, planned_keys))

        matched = set(matches.values())
        for rid, rec in self.state.events.items():
            if rid not in matched:
                item = self._plan_not_in_sheet(rid, rec, names, view, now)
                if item:
                    items.append(item)
        # Games that are over (and whose LSP events are) need nothing more:
        # listing them all season would bury what does.
        items = [i for i in items if not self._finished(i, now)]
        _note_overlaps(items, view)
        self.sheet_warnings += self._lsp_warnings(view, now)
        self._log_new_warnings()
        for item in items:
            if item.event.note:
                item.message = "; ".join(m for m in (item.message, item.event.note) if m)
        return items

    def _lacks_workflow(self, b: dict, cid: str, e: dict) -> bool:
        """Whether a tool-made event has no workflow and one can be copied to
        it (see _fill_variable). Read by id before saying so: a list read may
        carry less than the event has."""
        if _workflow_size(e) or b.get("workflow_tried"):
            return False
        try:
            fresh = self.lsp.get_event(e["Id"]) if e.get("Id") else None
        except LspError:
            return False
        if fresh is None or _workflow_size(fresh):
            return False
        if self._workflow_template(cid, e.get("Id")) is None:
            self._warn_no_template(cid, b.get("channel_name") or cid)
            return False
        return True

    def _log_new_warnings(self) -> None:
        """Log each warning when it appears (they're in Status and alerts
        every pass), and again if it goes away and comes back."""
        keys = {w["key"] for w in self.sheet_warnings}
        for w in self.sheet_warnings:
            if w["key"] not in self._logged_warnings:
                log.warning("Needs attention: %s", w["text"])
        self._logged_warnings = keys

    def _note_gone(self, now_iso: str) -> None:
        """Mark bookings on channels LSP no longer lists with when that was
        first seen (record_start ignores them), and unmark ones whose
        channel is back. Observed, not an action, so done in dry run too."""
        for rec in self.state.events.values():
            for cid, b in rec.get("bookings", {}).items():
                if cid in self._gone:
                    b.setdefault("gone_since", now_iso)
                else:
                    b.pop("gone_since", None)

    def _forget_gone(self) -> None:
        """Forget bookings whose channel has been missing from LSP for over
        GONE_FORGET (deleted for good, not a reply that left it out)."""
        cutoff = datetime.now(timezone.utc) - GONE_FORGET
        for rec in self.state.events.values():
            bookings = rec.get("bookings", {})
            for cid, b in list(bookings.items()):
                since = _parse_utc(b.get("gone_since"))
                if since and since < cutoff:
                    del bookings[cid]
                    log.warning("Forgot %r's booking on channel %s: it has been gone from LSP "
                                "since %s", rec.get("lsp_name") or rec.get("name"),
                                b.get("channel_name") or cid, b["gone_since"])

    def _lsp_warnings(self, view: _LspView, now: datetime) -> list[dict]:
        """LSP behaving in ways the sync relies on it not to (see the README),
        as warnings for Status and alerts."""
        out = []
        skew = getattr(self.lsp, "clock_skew", None)
        if skew is not None and abs(skew) > _CLOCK_SKEW_SECONDS:
            out.append({"key": "clock", "text": (
                f"This server's clock is {abs(skew):.0f} seconds "
                f"{'ahead of' if skew > 0 else 'behind'} LSP's: check NTP on both. When an "
                "event has started is judged by this clock")})
        evs = [e for evs in view.by_channel.values() for e in evs]
        early = [e for e in evs if _status(e) == "queued"
                 and (s := _parse_utc(e.get("Start"))) and s > now + _EARLY_QUEUE]
        if early:
            out.append({"key": "queued-early", "text": (
                f"LSP shows {len(early)} event(s) as Queued more than "
                f"{int(_EARLY_QUEUE.total_seconds() // 60)} minutes before they start "
                f"(e.g. “{early[0].get('Name')}”). The tool treats Queued events as started and "
                "stops applying sheet changes to them; if LSP queues events early, that needs "
                "changing in the tool")})
        naive = next((e for e in evs if isinstance(e.get("Start"), str)
                      and (p := _parse_any(e["Start"])) and p.tzinfo is None), None)
        if naive:
            out.append({"key": "naive-times", "text": (
                f"LSP sends event times without a time zone (e.g. “{naive['Start']}”), so the "
                "tool can't tell UTC from local time; same-named games exactly the UTC offset "
                "apart could be confused")})
        return out

    def _finished(self, item: PlanItem, now: datetime) -> bool:
        if item.status not in (EXISTS, OUT_OF_WINDOW, NO_CHANNEL) or item.event.end > now:
            return False
        rec = self.state.events.get(item.record_id) if item.record_id else None
        end = _parse_utc(rec.get("end")) if rec else None
        return rec is None or (end is not None and end < now)

    def _match(self, events: list[ScheduledEvent], now: datetime) -> dict[int, str]:
        """Pair sheet events with tracked records: index -> record id.

        1. same tab + date + name, and the same game that day (a time or PCR
           change; see ScheduledEvent.is_same_game for doubleheaders);
        2. same tab + name, when exactly one unmatched event and one unmatched
           record share it (a date change);
        3. same tab + column + date, likewise (a name change);
        4. same name + date + sheet start in another tab, likewise (the tab
           was renamed, or the game moved to another tab).
        Step 2 only considers records that haven't started, so a finished
        game never swallows a later rematch. Steps 3-4 also consider games
        in progress, so fixing a name after kickoff doesn't create a second
        recording of it.
        """
        out: dict[int, str] = {}
        used: set[str] = set()
        same_day: dict[tuple, list[str]] = defaultdict(list)
        for rid, rec in self.state.events.items():
            same_day[(rec["tab"], rec["date"], name_key(rec["name"]))].append(rid)
        for i, ev in enumerate(events):
            for rid in same_day.get((ev.source_tab, ev.event_date, name_key(ev.name)), []):
                rec = self.state.events[rid]
                if rid not in used and ev.is_same_game(rec.get("occurrence", 0),
                                                       rec.get("same_day", 0),
                                                       rec.get("sheet_start", "")):
                    out[i] = rid
                    used.add(rid)
                    break

        def not_started(rec):
            start = record_start(rec)
            return start is not None and start > now

        def not_ended(rec):
            end = _parse_utc(rec.get("end"))
            return end is not None and end > now

        loose = [
            (lambda ev: (ev.source_tab, name_key(ev.name)),
             lambda rec: (rec["tab"], name_key(rec["name"])), not_started),
            (lambda ev: (ev.source_tab, ev.source_column, ev.event_date),
             lambda rec: (rec["tab"], rec.get("column"), rec["date"]), not_ended),
            (lambda ev: (name_key(ev.name), ev.event_date, ev.sheet_start),
             lambda rec: (name_key(rec["name"]), rec["date"], rec.get("sheet_start")),
             lambda rec: bool(rec.get("sheet_start")) and not_ended(rec)),
        ]
        for ev_key, rec_key, eligible in loose:
            ev_groups: dict[tuple, list[int]] = defaultdict(list)
            for i, ev in enumerate(events):
                if i not in out:
                    ev_groups[ev_key(ev)].append(i)
            rec_groups: dict[tuple, list[str]] = defaultdict(list)
            for rid, rec in self.state.events.items():
                if rid not in used and eligible(rec):
                    rec_groups[rec_key(rec)].append(rid)
            for k, idxs in ev_groups.items():
                rids = rec_groups.get(k, [])
                if len(idxs) == 1 and len(rids) == 1:
                    out[idxs[0]] = rids[0]
                    used.add(rids[0])
        return out

    def _plan_new(self, ev, by_pcr, view: _LspView, planned_keys) -> PlanItem:
        """A sheet event the tool isn't tracking yet."""
        name = self.cfg.scheduling.lsp_name(ev)
        chans = by_pcr.get(ev.pcr) or []
        if not self._within_window(ev):
            return PlanItem(ev, name, OUT_OF_WINDOW,
                            [Target(cid, cname, OUT_OF_WINDOW) for cid, cname in chans],
                            message="Start is outside the active window")
        if not chans:
            return PlanItem(ev, name, NO_CHANNEL,
                            message=f"No LSP channels match PCR {ev.pcr or '(none)'}")

        targets = [self._new_target(cid, cname, name, ev, view, planned_keys)
                   for cid, cname in chans]
        if any(t.status == ERROR for t in targets):
            return PlanItem(ev, name, ERROR, targets, message="Could not read LSP; skipped this pass")
        to_create = sum(t.status == CREATE for t in targets)
        if to_create:
            msg = (f"Will be created on all {len(targets)} channels" if to_create == len(targets)
                   else f"Will be created on {to_create} of {len(targets)} channels")
            return PlanItem(ev, name, CREATE, targets, message=msg)
        owned = sum(t.message == _OWNED for t in targets)
        return PlanItem(ev, name, EXISTS, targets,
                        message=(f"Not added on {owned} channel(s): {_OWNED}" if owned
                                 else f"Already on all {len(targets)} channels"))

    def _new_target(self, cid, cname, name, ev, view: _LspView, planned_keys) -> Target:
        """What to do on a channel an event isn't booked on yet: create it,
        but never add a second copy of one already there."""
        key = (cid, name_key(name), _minute_key(ev.start))
        if key in planned_keys:
            return Target(cid, cname, EXISTS, "Same event earlier in the sheet")
        if cid in view.failed:
            return Target(cid, cname, ERROR, "Could not read this channel from LSP")
        # A hand-made event may carry the sheet's own wording ("MSOC vs UNC").
        found = view.find(cid, name, ev.start) or view.find(
            cid, f"{self.cfg.scheduling.event_name_prefix}{ev.name}", ev.start)
        if found and found.get("Id") in getattr(self, "_owned", {}):
            return Target(cid, cname, EXISTS, _OWNED, event_id=found.get("Id"))
        if found:
            return Target(cid, cname, EXISTS, "Already in LSP (made by hand; left alone)",
                          event_id=found.get("Id"), adopt=_booking(found, cname, by_tool=False))
        planned_keys.add(key)
        return Target(cid, cname, CREATE, "Will be created")

    def _plan_tracked(self, ev, rid, by_pcr, names, view: _LspView, now,
                      planned_keys) -> PlanItem:
        """A sheet event the tool already scheduled: bring LSP in line with it."""
        rec = self.state.events[rid]
        name = self.cfg.scheduling.lsp_name(ev)
        bookings = rec.get("bookings", {})

        def cname(cid):
            return names.get(cid) or bookings.get(cid, {}).get("channel_name") or cid

        def as_is(status, msg):
            return [Target(cid, cname(cid), status, msg, event_id=b.get("event_id"))
                    for cid, b in bookings.items()]

        if rec.get("locked"):
            reason = rec.get("lock_reason") or "changed in LSP"
            return PlanItem(ev, name, LOCKED, as_is(LOCKED, reason), record_id=rid, locked=True,
                            message=f"Locked: {reason}. Sheet changes are not applied.")
        # Started events are never changed, so there's no hand edit to look
        # for (LSP itself may rewrite a recording's End when it's stopped).
        # Nor are ones about to start, or ones LSP has queued or started
        # already (an engineer started it early).
        started = record_start(rec)
        live = _live_status(rec, view)
        if (started and started <= now + START_MARGIN) or live:
            msg = (f"{live.capitalize()} in LSP" if live
                   else "Started" if started <= now else "About to start")
            return PlanItem(ev, name, EXISTS, as_is(EXISTS, msg), record_id=rid,
                            started=True,
                            message=f"{msg}; sheet changes are no longer applied")
        want = {"Name": name, "Start": ev.start.isoformat(), "End": ev.end.isoformat()}
        edit = self._hand_edit(rec, view, now, want)
        if edit is None:
            return PlanItem(ev, name, ERROR, as_is(ERROR, "Could not read LSP"), record_id=rid,
                            message="Could not read LSP; skipped this pass")
        if edit:
            return PlanItem(ev, name, LOCKED, as_is(LOCKED, edit), record_id=rid, new_lock=edit,
                            message=f"Will lock: {edit}. Sheet changes won't be applied.")
        if not self._within_window(ev):
            # Most likely a typo (a year off): moving the booking there would
            # silently drop the real recording. Leave LSP as it is; fixing
            # the sheet brings the event back in line.
            return PlanItem(ev, name, OUT_OF_WINDOW, as_is(EXISTS, "Left as is"), record_id=rid,
                            message=f"Start {ev.start:%Y-%m-%d %H:%M} is outside the active "
                                    "window; LSP left as it was. If that's a typo, fix the sheet.")

        chans = by_pcr.get(ev.pcr) or []
        if not chans:
            return PlanItem(ev, name, NO_CHANNEL, as_is(EXISTS, "Left as is"), record_id=rid,
                            message=f"No LSP channels match PCR {ev.pcr or '(none)'}; "
                                    "existing bookings left as they are")
        key = (name_key(name), _minute_key(ev.start))
        targets, wanted = [], set()
        for cid, cn in chans:
            wanted.add(cid)
            b = bookings.get(cid)
            hand = None if b is None or b.get("by_tool") else view.by_id.get(b.get("event_id"))
            if b is not None and not b.get("by_tool") and hand is None and cid not in view.failed:
                b = None  # the hand-made event is gone from LSP: book it like any other
            if b is None:
                # A new channel for this event (its PCR changed, or the hand-
                # made event there was deleted).
                targets.append(self._new_target(cid, cn, name, ev, view, planned_keys))
            elif not b.get("by_tool"):
                e = hand[1] if hand else {}
                differs = hand and ((e.get("Name") or "").strip() != name.strip()
                                    or not _same_instant(e.get("Start"), ev.start)
                                    or not _same_instant(e.get("End"), ev.end))
                targets.append(Target(cid, cn, EXISTS,
                                      _HAND_DIFFERS if differs else "Made by hand in LSP; left alone",
                                      event_id=b.get("event_id")))
            else:
                # Untouched by hand (checked above), so LSP's current values
                # are what the tool last wrote; the snapshot is the fallback.
                hit = _locate(b, cid, view)
                cur = hit[1] if hit else (b.get("snapshot") or {})
                diffs = []
                if (cur.get("Name") or "").strip() != name.strip() and not _kept_as_written(b, cur, name):
                    diffs.append("name")
                if not _same_instant(cur.get("Start"), ev.start):
                    diffs.append("start")
                if not _same_instant(cur.get("End"), ev.end):
                    diffs.append("end")
                var = self.cfg.lsp.event_name_variable
                if (hit and var and b.get("variable_sent") != name
                        and _with_variable(hit[1], var, name) is not None):
                    diffs.append(var)
                if hit and self._lacks_workflow(b, cid, hit[1]):
                    diffs.append("workflow")
                event_id = hit[1].get("Id") if hit else b.get("event_id")
                if {"name", "start"} & set(diffs):
                    # Moving it onto another event with the same name and
                    # start would record the same thing twice.
                    other = view.find(cid, name, ev.start)
                    if (cid, *key) in planned_keys or (other and other.get("Id") != event_id):
                        targets.append(Target(cid, cn, EXISTS, _DUPLICATE, event_id=event_id))
                        continue
                    planned_keys.add((cid, *key))
                targets.append(Target(cid, cn, UPDATE if diffs else EXISTS,
                                      f"Will update {', '.join(diffs)}" if diffs else "Up to date",
                                      event_id=event_id))
        for cid, b in bookings.items():
            if cid in getattr(self, "_gone", ()):
                targets.append(Target(cid, cname(cid), EXISTS, _GONE,
                                      event_id=b.get("event_id")))
                continue
            if cid in wanted or not b.get("by_tool"):
                continue
            # By where it is now, in case LSP gave the event a new id.
            hit = _locate(b, cid, view)
            event_id = hit[1].get("Id") if hit else b.get("event_id")
            # The PCR the booking was made for (older bookings: the record's).
            if (b.get("pcr") or rec.get("pcr") or "") != (ev.pcr or ""):
                targets.append(Target(cid, cname(cid), DELETE,
                                      "PCR changed; will be removed from this channel",
                                      event_id=event_id))
            else:
                # Same PCR, but the channel no longer matches it (renamed in
                # LSP, or the PCR's channel match was edited). Not a reason
                # to delete a booking; an engineer can remove it in LSP.
                targets.append(Target(cid, cname(cid), EXISTS,
                                      f"No longer matches PCR {ev.pcr}; left in LSP",
                                      event_id=event_id))

        counts = defaultdict(int)
        for t in targets:
            counts[t.status] += 1
        if counts[ERROR]:
            return PlanItem(ev, name, ERROR, targets, record_id=rid,
                            message="Could not read LSP; skipped this pass")
        # Channels left as they are although they don't match the sheet.
        notes = [(n, why) for why in (_DUPLICATE, _OWNED, _HAND_DIFFERS, _GONE)
                 if (n := sum(t.message == why for t in targets))]
        if not (counts[CREATE] or counts[UPDATE] or counts[DELETE]):
            return PlanItem(ev, name, EXISTS, targets, record_id=rid,
                            message=("; ".join(f"Not changed on {n} channel(s): {why}"
                                               for n, why in notes)
                                     or f"Up to date on all {len(targets)} channels"))
        parts = [f"{verb} on {counts[s]}" for s, verb in
                 ((UPDATE, "update"), (CREATE, "create"), (DELETE, "remove")) if counts[s]]
        msg = "Sheet changed: will " + ", ".join(parts) + " channel(s)"
        for n, why in notes:
            msg += f"; not changed on {n}: {why}"
        return PlanItem(ev, name, UPDATE, targets, record_id=rid, message=msg)

    def _plan_not_in_sheet(self, rid, rec, names, view, now) -> Optional[PlanItem]:
        """A tracked event that is gone from the sheet (column removed, date
        TBD, tab turned off, ignored...). Never deleted automatically."""
        start, end = _parse_utc(rec.get("start")), _parse_utc(rec.get("end"))
        if not start or (end or start) < now:
            return None
        ev = ScheduledEvent(rec["name"], rec.get("pcr") or "", start, end or start, rec["tab"],
                            rec.get("column") or 0, rec["date"], rec.get("occurrence", 0))
        targets = [Target(cid, names.get(cid) or b.get("channel_name") or cid, EXISTS,
                          "Left in LSP", event_id=b.get("event_id"))
                   for cid, b in rec.get("bookings", {}).items()]
        edit = ("" if rec.get("locked") or (record_start(rec) or start) <= now
                else (self._hand_edit(rec, view, now) or ""))
        msg = "No longer in the sheet; left in LSP (delete it there if it's cancelled)"
        if rec.get("locked") or edit:
            msg += f". Locked: {rec.get('lock_reason') or edit}"
        return PlanItem(ev, rec.get("lsp_name") or rec["name"], NOT_IN_SHEET, targets, msg,
                        record_id=rid, locked=bool(rec.get("locked")), new_lock=edit)

    def _hand_edit(self, rec, view: _LspView, now, want: Optional[dict] = None) -> Optional[str]:
        """Why this record's LSP events no longer match what the tool last
        wrote ("" = untouched), or None if LSP couldn't be read.

        A booking is untouched if LSP matches its snapshot *or* the values the
        tool last sent: the re-read right after a write can still return the
        old values, which would otherwise look like a hand edit next pass.
        Or if it holds exactly `want`, what the tool would write now: someone
        made the sheet's change in LSP first, which is no conflict.

        Canceling the event in LSP, or setting its event-name variable to
        something the tool didn't put there, is a hand edit too."""
        for cid, b in rec.get("bookings", {}).items():
            if not b.get("by_tool") or cid in getattr(self, "_gone", ()):
                continue  # hand-made bookings are never ours to compare
            label = b.get("channel_name") or cid
            if cid in view.failed:
                return None
            snap = b.get("snapshot")
            hit = _locate(b, cid, view)
            if hit is None:
                end = _parse_utc((snap or {}).get("End")) or _parse_utc(rec.get("end"))
                if end and end < now:
                    continue  # finished and cleared out of LSP
                # Not in the channel's events. Before calling it deleted (and
                # locking it), ask LSP for it by id: one reply that wrongly
                # left it out mustn't lock every event on the channel.
                try:
                    fresh = self.lsp.get_event(b["event_id"]) if b.get("event_id") else None
                except LspError:
                    return None
                if fresh is None:
                    return f"{label}: deleted or moved in LSP"
                hit = view.add(fresh, cid)
                log.warning("%r on %s was missing from LSP's list of the channel's events, but "
                            "LSP still has it", rec.get("lsp_name") or rec.get("name"), label)
            found_cid, e = hit
            if found_cid != cid:
                return f"{label}: moved to another channel in LSP"
            if _status(e) in _ACTIVE:
                continue  # LSP has started it: frozen, and LSP may change its times
            if _status(e) == "canceled":
                return f"{label}: canceled in LSP"
            if self._variable_edited(e, b):
                return f"{label}: {self.cfg.lsp.event_name_variable} changed in LSP"
            if (snap is None or _matches_written(e, b.get("written"))
                    or _matches_written(e, want)):
                continue  # no baseline yet (taken next pass), or as the tool left it
            if (e.get("Name") or "").strip() != (snap.get("Name") or "").strip():
                return f"{label}: name changed in LSP"
            for f in ("Start", "End"):
                if _lsp_time(e.get(f)) != _lsp_time(snap.get(f)):
                    return f"{label}: {f.lower()} changed in LSP"
        return ""

    def _variable_edited(self, e: dict, b: dict) -> bool:
        """Whether someone set the event's event-name variable by hand: it
        holds a value of its own that is neither its default nor one the tool
        sent or saw there after its own writes. Bookings with no record of
        the variable yet (made before the tool tracked it) aren't judged."""
        var = self.cfg.lsp.event_name_variable
        if not var or not ("variable_sent" in b or "variable_seen" in b):
            return False
        own = _variable_own(e, var)
        if not own:
            return False
        return own not in {(b.get(k) or "").strip() for k in ("variable_sent", "variable_seen")}

    def _note_variable(self, b: dict, e: dict) -> None:
        """Remember the event-name variable value LSP holds on an event the
        tool considers untouched, so a later hand change to it is noticed."""
        var = self.cfg.lsp.event_name_variable
        if var and _variable(e, var) is not None and not self._variable_edited(e, b):
            b["variable_seen"] = _variable_own(e, var)

    # -- execution ----------------------------------------------------------

    def run_once(self) -> dict:
        """Execute the plan. Counts are per channel, except `parsed` / window /
        no-channel / locked / not-in-sheet, which count sheet events."""
        summary = _summary(self.cfg.runtime.dry_run)
        try:
            items = self.plan()
        except (LspError, SheetError) as exc:
            log.exception("Could not build plan; aborting pass")
            summary["errors"] += 1
            summary["problems"].append(str(exc))
            return summary

        summary["parsed"] = sum(i.status != NOT_IN_SHEET for i in items)
        summary["errors"] += len(self.sheet_errors)
        summary["problems"].extend(self.sheet_errors)
        summary["warnings"] = self.sheet_warnings
        try:
            self._execute(items, summary, live=not self.cfg.runtime.dry_run)
            if not self.cfg.runtime.dry_run:
                self._forget_gone()
            self._prune()
        finally:
            self.state.save()  # keep what was written even if the pass broke off
        log.info(
            "Pass complete: parsed=%(parsed)d created=%(created)d updated=%(updated)d "
            "removed=%(deleted)d existing=%(skipped_existing)d locked=%(locked)d "
            "not-in-sheet=%(not_in_sheet)d out-of-window=%(skipped_window)d "
            "no-channel=%(no_channel)d errors=%(errors)d",
            summary,
        )
        return summary

    def send_one(self, tab: str, event_date: str, event_name: str, occurrence: int = 0) -> dict:
        """Apply one sheet event to LSP now (Preview's per-event send, for testing).

        Always live, even with dry run on: it is an explicit, single-event
        action and the background loop stays dry. Uses the same plan as a
        pass, so it creates or updates exactly what a pass would, and a later
        pass won't repeat it.
        """
        want = (event_date, name_key(event_name), occurrence)
        item = next((i for i in self.plan()
                     if i.status != NOT_IN_SHEET and i.event.source_tab == tab
                     and i.event.override_key() == want), None)
        if item is None:
            raise LspError("That event is no longer in the sheet — refresh the preview")
        if item.status not in (CREATE, UPDATE):
            raise LspError(f"Nothing to send: {item.message or item.status}")
        summary = _summary(False)
        summary["name"] = item.lsp_name
        try:
            self._execute([item], summary, live=True, force=True)
        finally:
            self.state.save()
        log.info("Sent %r to LSP from preview: created=%d updated=%d removed=%d errors=%d",
                 item.lsp_name, summary["created"], summary["updated"], summary["deleted"],
                 summary["errors"])
        return summary

    def unlock(self, record_id: str) -> dict:
        """Let the tool manage a locked event again.

        LSP's current values become the new baseline, and bookings deleted or
        moved away in LSP are forgotten, so the next pass re-applies the sheet
        (updating the edited bookings back, event-name variable included, and
        re-creating deleted ones). An event canceled in LSP can't be unlocked:
        it is still there, so it would only lock again.
        """
        rec = self.state.record(record_id)
        if rec is None:
            raise LspError("That event isn't tracked any more — refresh the preview")
        exists = {c.get("Id") for c in self.lsp.get_all_channels()}
        view = _LspView(self.lsp, set(rec.get("bookings", {})) & exists)
        if view.failed:
            raise LspError("Could not read LSP; try again")
        # Bookings on channels LSP no longer lists are left as they are.
        hits = {cid: _locate(b, cid, view) for cid, b in rec["bookings"].items()
                if b.get("by_tool") and cid in exists}
        canceled = [rec["bookings"][cid].get("channel_name") or cid for cid, hit in hits.items()
                    if hit and hit[0] == cid and _status(hit[1]) == "canceled"]
        if canceled:
            raise LspError(f"It is canceled in LSP on {', '.join(canceled)}: delete it there "
                           "(the tool then books it again) and unlock it, or leave it locked")
        var = self.cfg.lsp.event_name_variable
        for cid, hit in hits.items():
            b = rec["bookings"][cid]
            if hit and hit[0] == cid:
                b.update(event_id=hit[1].get("Id"), snapshot=_snapshot(hit[1]))
                b.pop("variable_sent", None)  # sent again next pass
                if var and _variable(hit[1], var) is not None:
                    b["variable_seen"] = _variable_own(hit[1], var)
            else:
                del rec["bookings"][cid]
        rec.update(locked=False, lock_reason="", locked_at=None)
        if not rec["bookings"]:
            self.state.events.pop(record_id, None)
        self.state.save()
        log.info("Unlocked %r; the sheet is applied to it again", rec.get("lsp_name") or rec["name"])
        return {"name": rec.get("lsp_name") or rec["name"]}

    def unlock_all(self) -> dict:
        """Unlock every locked event (after an LSP-wide change, say, that
        locked them all). Ones that can't be unlocked (canceled in LSP) are
        left locked and named."""
        out = {"unlocked": 0, "failed": []}
        for rid in [rid for rid, rec in self.state.events.items() if rec.get("locked")]:
            try:
                self.unlock(rid)
                out["unlocked"] += 1
            except LspError as exc:
                rec = self.state.events.get(rid) or {}
                out["failed"].append(f"{rec.get('lsp_name') or rec.get('name')}: {exc}")
        return out

    def _execute(self, items: list[PlanItem], summary: dict, live: bool, force: bool = False) -> None:
        """Apply `items` to LSP (or just log them when not `live`), recording
        the results in state and counting into `summary`. `force` lets the
        LSP client write even when it was built read-only."""
        touched: list[tuple[str, str]] = []  # (record id, channel id) to rebaseline
        now = _now_iso()
        for item in items:
            self.heartbeat()
            if self.stop.is_set():
                log.warning("Shutting down: stopping the pass before %r", item.lsp_name)
                summary["problems"].append("Pass stopped early for shutdown")
                break
            if item.new_lock and item.record_id:
                # Observed, not an action, so it's recorded in dry run too.
                self.state.lock(item.record_id, item.new_lock, now)
                log.warning("Locked %r: %s. The tool won't change it again until it is "
                            "unlocked in the UI.", item.lsp_name, item.new_lock)
            if item.status == OUT_OF_WINDOW:
                summary["skipped_window"] += 1
                continue
            if item.status == NO_CHANNEL:
                summary["no_channel"] += 1
                log.info("%s: %s", item.lsp_name, item.message)
                continue
            if item.status == NOT_IN_SHEET:
                summary["not_in_sheet"] += 1
                continue
            if item.status == ERROR:
                summary["errors"] += 1
                continue

            rid = item.record_id
            if rid and not (item.new_lock or item.locked):
                self._refresh_baselines(rid, None if item.started else _written(item))
            if item.status == LOCKED:
                summary["locked"] += 1
                continue

            failed = False
            for t in item.targets:
                if t.status == EXISTS:
                    summary["skipped_existing"] += 1
                    if t.adopt is not None and live:
                        rid = rid or self._new_record(item)
                        self.state.events[rid]["bookings"][t.channel_id] = t.adopt
                    continue
                if t.status not in (CREATE, UPDATE, DELETE):
                    continue
                if not live:
                    log.info("[DRY RUN] would %s %r on %s (PCR %s) %s -> %s", t.status,
                             item.lsp_name, t.channel_name, item.event.pcr,
                             item.event.start.isoformat(), item.event.end.isoformat())
                    summary[_COUNTER[t.status]] += 1
                    continue
                try:
                    why = self._unsafe_now(item, t, rid) if t.status != CREATE else ""
                    if why:
                        # LSP still has the old values here, so the record
                        # keeps them; next pass decides afresh (and locks it
                        # if someone changed it).
                        log.warning("Not changing %r on %s: %s", item.lsp_name, t.channel_name, why)
                        summary["problems"].append(f"{item.lsp_name} on {t.channel_name}: {why}; "
                                                   "left as is")
                        failed = True
                        continue
                    rid = self._apply(item, t, rid, force, now, touched)
                except LspError:
                    log.exception("Failed to %s %r on %s", t.status, item.lsp_name, t.channel_name)
                    if t.status == CREATE:
                        rid, made = self._recover_create(item, t, rid, force, now, touched)
                        if made:
                            summary["created"] += 1
                            self._record_write()
                            continue
                    summary["errors"] += 1
                    failed = True
                    continue
                summary[_COUNTER[t.status]] += 1
                self._record_write()

            # A started event's record keeps the times it started with: taking
            # the sheet's new ones would make it look not started next pass,
            # and the tool would move an event that is already recording. So
            # does one whose write failed somewhere: LSP still has the old
            # times there, and "started" is judged from them.
            if live and rid and not item.started:
                fields = _record_fields(item)
                if failed:
                    fields = {k: v for k, v in fields.items() if k not in ("start", "end")}
                self.state.events[rid].update(fields)
        self._rebaseline(touched)

    def _record_write(self) -> None:
        """Save state right after each LSP write, so a restart mid-pass
        can't leave events in LSP that the tool has no record of (the next
        pass would take them for hand-made ones and stop updating them).
        The record's own fields are only refreshed once the whole item is
        done, so an interrupted PCR move still looks like one next pass."""
        self.state.save()

    def _unsafe_now(self, item: PlanItem, t: Target, rid: Optional[str]) -> str:
        """Why an UPDATE / DELETE planned earlier in this pass mustn't go
        ahead now ("" = it may): the event is about to start (a pass takes a
        while), or LSP, read again just before the write, shows it started,
        canceled, or changed since the pass read it (someone is editing it
        right now). Raises LspError if it can't be read."""
        rec = self.state.events.get(rid) if rid else None
        start = record_start(rec) if rec else None
        if start and start <= datetime.now(timezone.utc) + START_MARGIN:
            return "about to start"
        fresh = self.lsp.get_event(t.event_id)
        if fresh is None:
            return "" if t.status == DELETE else "no longer in LSP"
        status = _status(fresh)
        if status in _ACTIVE or status == "canceled":
            return f"{status} in LSP"
        view = getattr(self, "_view", None)
        seen = view.by_id.get(t.event_id) if view else None
        if seen is not None:
            before = seen[1]
            var = self.cfg.lsp.event_name_variable
            if ((fresh.get("Name") or "").strip() != (before.get("Name") or "").strip()
                    or any(not _same_lsp_time(fresh.get(f), before.get(f)) for f in ("Start", "End"))
                    or (fresh.get("ChannelId") and fresh.get("ChannelId") != before.get("ChannelId"))
                    or (var and _variable(before, var) is not None
                        and _variable_own(fresh, var) != _variable_own(before, var))):
                return "changed in LSP during this pass"
        return ""

    def _apply(self, item: PlanItem, t: Target, rid, force, now, touched) -> Optional[str]:
        """Carry out one CREATE / UPDATE / DELETE target; returns the record id."""
        ev = item.event
        if t.status == CREATE:
            result = self.lsp.add_event(ev, t.channel_id, item.lsp_name, force=force)
            created = self._created_event(result, item, t)
            rid = self._record_created(item, t, rid, created, force, now, touched)
        elif t.status == UPDATE:
            self.lsp.patch_event(t.event_id, item.lsp_name, ev.start, ev.end, force=force)
            b = self.state.events[rid]["bookings"][t.channel_id]
            b.update(event_id=t.event_id, name=item.lsp_name, snapshot=None,
                     written=_written(item), updated_at=now, pcr=ev.pcr)
            touched.append((rid, t.channel_id))
            log.info("Updated %r on %s (%s) @ %s", item.lsp_name, t.channel_name,
                     t.message, ev.start.isoformat())
            self._fill_variable(item, t, t.event_id, rid, force)
        elif t.status == DELETE:
            self.lsp.remove_event(t.event_id, force=force)
            self.state.events[rid]["bookings"].pop(t.channel_id, None)
            log.info("Removed %r from %s (PCR now %s)", item.lsp_name, t.channel_name, ev.pcr)
        return rid

    def _record_created(self, item: PlanItem, t: Target, rid, created: Optional[dict],
                        force, now, touched) -> str:
        """Record a booking for an event the tool just created (`created` is
        it as LSP holds it, if found) and fill its variable; returns the
        record id."""
        event_id = created.get("Id") if created else None
        rid = rid or self._new_record(item)
        self.state.events[rid]["bookings"][t.channel_id] = {
            "event_id": event_id, "channel_name": t.channel_name, "by_tool": True,
            "created_at": now, "name": item.lsp_name, "pcr": item.event.pcr,
            "snapshot": _snapshot(created) if created else None,
            "written": _written(item),
        }
        touched.append((rid, t.channel_id))
        log.info("Created %r on %s (PCR %s) @ %s (id=%s)", item.lsp_name, t.channel_name,
                 item.event.pcr, item.event.start.isoformat(), event_id)
        if event_id:
            self._fill_variable(item, t, event_id, rid, force)
        return rid

    def _recover_create(self, item: PlanItem, t: Target, rid, force, now,
                        touched) -> tuple[Optional[str], bool]:
        """After a failed AddEvent (e.g. a timeout), check whether LSP made
        the event anyway. If it did, record it as the tool's, so a later pass
        doesn't take it for a hand-made one. Returns (record id, found)."""
        try:
            found = self._find_new_event(item, t)
        except LspError:
            return rid, False
        if found is None:
            return rid, False
        log.warning("AddEvent for %r on %s failed, but LSP created the event anyway; tracking it",
                    item.lsp_name, t.channel_name)
        return self._record_created(item, t, rid, found, force, now, touched), True

    def _find_new_event(self, item: PlanItem, t: Target) -> Optional[dict]:
        """The event on the channel with this name and start that wasn't
        there when this pass read LSP."""
        view = getattr(self, "_view", None)
        known = {e.get("Id") for e in (view.by_channel.get(t.channel_id, []) if view else [])}
        for e in self.lsp.get_events_for_channel(t.channel_id):
            if (e.get("Id") not in known
                    and (e.get("Name") or "").strip() == item.lsp_name.strip()
                    and _same_instant(e.get("Start"), item.event.start)):
                return e
        return None

    def _created_event(self, result, item: PlanItem, t: Target) -> Optional[dict]:
        """The event AddEvent just made, as LSP now holds it: LSP copies the
        channel's workflow variables into it (e.g. Event Name = its default)
        before replying. Found by the id in the reply, or, if the reply has
        none, as the new event on the channel with this name and start."""
        event_id = _event_id(result)
        try:
            if event_id:
                return self.lsp.get_event(event_id) or (result if isinstance(result, dict) else None)
            log.warning("AddEvent's reply for %r on %s has no event id (%s); finding the new "
                        "event on the channel", item.lsp_name, t.channel_name, _describe(result))
            found = self._find_new_event(item, t)
            if found is not None:
                return found
        except LspError as exc:
            log.warning("Could not read the new event %r on %s back from LSP: %s",
                        item.lsp_name, t.channel_name, exc)
            return result if isinstance(result, dict) else None
        log.warning("Could not find the new event %r on %s in LSP", item.lsp_name, t.channel_name)
        return None

    def _fill_variable(self, item: PlanItem, t: Target, event_id: str,
                       rid: str, force: bool) -> None:
        """Make sure a tool-made event has its channel's workflow, and set its
        event-name variable to its name; then read it back to check LSP kept
        them. A failure is logged and never undoes the create / update.

        LSP used to copy the channel's workflow (Vantage variables, the
        workflow's description) into every new event; when it doesn't, the
        event has none (its variable tab greyed out in LSP) and Vantage gets
        nothing. Then the workflow is copied from the most complete event on
        the same channel (see _workflow_template), every value cleared so the
        channel's defaults apply, as on a fresh event.

        The event is read by id first, and what's sent back is copied from
        that read, never from a list of events or a reply: LSP replaces the
        part it's sent whole, so a partial copy would wipe the event's other
        workflow variables and description. The variable is set once per
        name, so a value LSP won't keep isn't re-sent every pass."""
        var = self.cfg.lsp.event_name_variable
        try:
            current = self.lsp.get_event(event_id)
        except LspError as exc:
            log.warning("Could not read %r on %s to check its workflow (tried again next pass): %s",
                        item.lsp_name, t.channel_name, exc)
            return
        if not current:
            return
        b = self.state.events[rid]["bookings"][t.channel_id]
        fields: dict = {}
        template = None
        if _workflow_size(current) == 0:
            template = self._workflow_template(t.channel_id, event_id)
            if template is None:
                self._warn_no_template(t.channel_id, t.channel_name)
            else:
                fields = _fresh_workflow(template)
        base = {**current, **fields}
        var_fields = _with_variable(base, var, item.lsp_name) if var else None
        if var_fields:
            fields.update(var_fields)
            b["variable_sent"] = item.lsp_name
        if not fields:
            return
        ev = item.event
        try:
            self.lsp.patch_event(event_id, item.lsp_name, ev.start, ev.end,
                                 force=force, extra=fields)
        except LspError as exc:
            log.warning("Could not set the workflow / %r on %r on %s: %s", var, item.lsp_name,
                        t.channel_name, exc)
            return
        try:
            after = self.lsp.get_event(event_id) or {}
        except LspError as exc:
            log.warning("Could not read %r on %s back from LSP: %s", item.lsp_name,
                        t.channel_name, exc)
            return
        if template is not None:
            if _workflow_size(after):
                log.info("Set %r's workflow on %s (copied from %r, values cleared to the "
                         "channel's defaults): LSP didn't give it one", item.lsp_name,
                         t.channel_name, template.get("Name"))
            else:
                b["workflow_tried"] = True  # LSP won't keep it: don't resend every pass
                log.warning("LSP didn't keep the workflow sent for %r on %s", item.lsp_name,
                            t.channel_name)
        if var_fields:
            b["variable_seen"] = _variable_own(after, var)  # a value LSP rewrote isn't a hand edit
            holds = _variable_value(after, var)
            if holds == item.lsp_name:
                log.info("Set %r = %r on %s", var, item.lsp_name, t.channel_name)
            else:
                log.warning("LSP didn't keep %r = %r on %s: it holds %r",
                            var, item.lsp_name, t.channel_name, holds)

    def _workflow_template(self, channel_id: str, exclude_id: Optional[str]) -> Optional[dict]:
        """The most complete workflow on another event on this channel (the
        one with the most variables and parameters; the newest of those), read
        by id. None if no event there has one. Cached for the pass."""
        cache = self.__dict__.setdefault("_templates", {})
        if channel_id in cache:
            t = cache[channel_id]
            return t if t is None or t.get("Id") != exclude_id else None
        view = getattr(self, "_view", None)
        evs = [e for e in (view.by_channel.get(channel_id, []) if view else [])
               if e.get("Id") and e.get("Id") != exclude_id]
        evs.sort(key=lambda e: (_workflow_size(e), e.get("Start") or ""), reverse=True)
        best = None
        # A list read may carry less than the event itself: read the likeliest
        # few by id (most complete first, then the newest).
        if not any(_workflow_size(e) for e in evs):
            evs.sort(key=lambda e: e.get("Start") or "", reverse=True)
        for e in evs[:_TEMPLATE_READS]:
            try:
                full = self.lsp.get_event(e["Id"]) or e
            except LspError:
                full = e
            if _workflow_size(full) > _workflow_size(best or {}):
                best = full
        cache[channel_id] = best
        return best

    def _warn_no_template(self, channel_id: str, channel_name: str) -> None:
        w = {"key": f"no-workflow:{channel_id}", "text": (
            f"Events the tool creates on {channel_name} get no workflow (variables, description) "
            "from LSP, and no event on that channel has one to copy: create one event there by "
            "hand in LSP, and the tool copies its workflow from then on")}
        if all(x["key"] != w["key"] for x in self.sheet_warnings):
            self.sheet_warnings.append(w)
            log.warning("%s", w["text"])

    def _refresh_baselines(self, rid: str, want: Optional[dict] = None) -> None:
        """For an untouched record's bookings: follow an event LSP gave a new
        id, and re-snapshot one whose last re-read failed or came back stale
        (LSP now shows what the tool wrote), or that someone already set to
        `want`, the sheet's values."""
        view = getattr(self, "_view", None)
        if view is None:
            return
        for cid, b in self.state.events[rid].get("bookings", {}).items():
            if not b.get("by_tool"):
                continue
            hit = _locate(b, cid, view)
            if not hit or hit[0] != cid:
                continue
            e = hit[1]
            if e.get("Id") and e["Id"] != b.get("event_id"):
                b["event_id"] = e["Id"]
            if (b.get("snapshot") is None or _matches_written(e, b.get("written"))
                    or _matches_written(e, want)):
                b["snapshot"] = _snapshot(e)
            self._note_variable(b, e)

    def _new_record(self, item: PlanItem) -> str:
        return self.state.new_record(_record_fields(item))

    def _rebaseline(self, touched: list[tuple[str, str]]) -> None:
        """Snapshot what LSP now holds for bookings the tool just wrote, so a
        later difference means a hand edit. Uses the pass's view for adopted
        bookings and re-reads channels the tool changed."""
        if not touched:
            return
        fresh = _LspView(self.lsp, {cid for _, cid in touched})
        for rid, cid in touched:
            b = self.state.events.get(rid, {}).get("bookings", {}).get(cid)
            if not b:
                continue
            hit = fresh.by_id.get(b.get("event_id"))
            if hit:
                b["snapshot"] = _snapshot(hit[1])
                self._note_variable(b, hit[1])
            # Otherwise keep what the write returned (or None: baseline next pass).

    def _prune(self) -> None:
        """Forget records of events that ended longer ago than LSP keeps
        events (its EventCleanupThresholdInDays setting), so the state file
        doesn't grow forever."""
        days = self._cleanup_days()
        dropped = self.state.prune(datetime.now(timezone.utc) - timedelta(days=days))
        if dropped:
            log.info("Forgot %d event(s) that ended over %d days ago (LSP's event cleanup)",
                     dropped, days)

    def _cleanup_days(self) -> int:
        """LSP's event cleanup threshold in days, or DEFAULT_CLEANUP_DAYS if
        it can't be read (e.g. the login lacks lsp-config-read)."""
        try:
            days = int(self.lsp.get_general_settings().get("EventCleanupThresholdInDays") or 0)
        except (LspError, TypeError, ValueError) as exc:
            days, why = 0, str(exc)
        else:
            why = "not set"
        if days > 0:
            return days
        if not getattr(self, "_cleanup_warned", False):
            log.warning("Could not read LSP's event cleanup threshold (%s); keeping records "
                        "for %d days after their events end", why, DEFAULT_CLEANUP_DAYS)
            self._cleanup_warned = True
        return DEFAULT_CLEANUP_DAYS

    # -- event name variable ------------------------------------------------

    def event_name_channels(self) -> dict:
        """For each mapped PCR channel: whether its events carry the
        event-name variable (read from its newest events), so the tool can set
        it. Returns {"variable", "channels": [{id, name, pcr, status,
        message}]}; status is found / missing / error, or off when no
        variable is configured."""
        variable = self.cfg.lsp.event_name_variable
        pcrs: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        for pcr, found in self._resolve_channels().items():
            for cid, cname in found:
                pcrs.setdefault(cid, []).append(pcr)
                names[cid] = cname
        out = []
        for cid in sorted(pcrs, key=lambda c: names[c].lower()):
            row = {"id": cid, "name": names[cid], "pcr": "/".join(sorted(pcrs[cid]))}
            out.append(row)
            if not variable:
                row.update(status="off", message="No event name variable set")
                continue
            try:
                events = self.lsp.get_events_for_channel(cid)
            except LspError:
                row.update(status=ERROR, message="Could not read its events from LSP")
                continue
            events.sort(key=lambda e: e.get("Start") or "", reverse=True)
            hit = next((f for f in (_variable(e, variable) for e in events) if f), None)
            if hit:
                default = _variable_default(*hit)
                row.update(status="found", message=f"Default “{default}”" if default else "")
                continue
            seen = [n for n in dict.fromkeys(n for e in events[:20] for n in _variable_names(e)) if n]
            row.update(status="missing", message=(
                "No events on this channel to check yet" if not events
                else f"Its events have: {', '.join(seen)}" if seen
                else "Its events carry no variables"))
        return {"variable": variable, "channels": out}

    # -- cleanup (testing) --------------------------------------------------

    def scheduled_events(self, past_days: int = 0) -> dict:
        """Live view of events in LSP on the mapped PCR channels.

        Returns events whose end is after now - past_days, each flagged with
        whether this tool created it (and whether it's locked), plus any
        tool-created events that are no longer found in LSP (deleted or moved
        by hand).
        """
        channels = self.lsp.get_all_channels()
        pcrs_by_channel: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        for pcr, found in self._resolve_channels(channels).items():
            for cid, cname in found:
                pcrs_by_channel.setdefault(cid, []).append(pcr)
                names[cid] = cname
        tool_ids = {info["event_id"]: info for _k, info in self.state.tool_created()}
        variable = self.cfg.lsp.event_name_variable
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, past_days))

        events, all_ids = [], set()
        for channel_id, pcrs in pcrs_by_channel.items():
            for e in self.lsp.get_events_for_channel(channel_id):
                all_ids.add(e.get("Id"))
                end = _parse_utc(e.get("End") or e.get("Start"))
                if end is None or end < cutoff:
                    continue
                info = tool_ids.get(e.get("Id"))
                events.append({
                    "id": e.get("Id"),
                    "name": e.get("Name"),
                    "pcr": "/".join(sorted(pcrs)),
                    "channel": names[channel_id],
                    "start": e.get("Start"),
                    "end": e.get("End"),
                    "status": e.get("Status"),
                    "created_by": e.get("CreatedByDisplayName"),
                    "variable": _variable_value(e, variable),
                    "by_tool": info is not None,
                    "locked": bool(info and info.get("locked")),
                })
        events.sort(key=lambda ev: _parse_utc(ev["start"]) or cutoff)

        # Tool-created events in this window not found on any mapped channel
        # (deleted or moved by hand in LSP, or their PCR is no longer mapped).
        missing = []
        for event_id, info in tool_ids.items():
            end = _parse_utc(info.get("end"))
            if event_id in all_ids or (end is not None and end < cutoff):
                continue
            missing.append({"id": event_id, "name": info.get("name"),
                            "pcr": info.get("pcr"), "source_tab": info.get("source_tab")})
        return {"events": events, "missing": missing, "channels": len(pcrs_by_channel),
                "variable": variable}

    def delete_created(self) -> dict:
        """Delete from LSP every upcoming event this tool created, then
        forget them.

        Only touches events tagged as tool-created in local state, so events
        already present in LSP (or made by hand) are never removed. Events
        that have started are skipped, so a recording in progress or a past
        one's history is never removed. Runs even with dry run on, so events
        sent from Preview for testing can be cleaned up without taking the
        loop live. Locked events (changed by hand in LSP) are left alone too;
        unlock one to include it. LSP is read first, so an event started
        early by hand, or changed by hand since the last pass, is skipped too.
        """
        summary = {"deleted": 0, "failed": 0, "errors": [], "skipped_locked": 0,
                   "skipped_started": 0}
        now = datetime.now(timezone.utc)
        created = self.state.tool_created()
        exists = {c.get("Id") for c in self.lsp.get_all_channels()}
        self._gone = {info["channel_id"] for _, info in created} - exists
        view = _LspView(self.lsp, {info["channel_id"] for _, info in created} - self._gone)
        for ref, info in created:
            event_id = info.get("event_id")
            if info["channel_id"] in self._gone:
                continue  # its channel is gone from LSP, and the event with it
            start = _parse_utc(info.get("start"))
            hit = view.by_id.get(event_id)
            if (start is None or start <= now + START_MARGIN
                    or (hit and _status(hit[1]) in _ACTIVE)):
                summary["skipped_started"] += 1
                continue
            rec = self.state.events.get(ref[0]) or {}
            if info.get("locked") or (rec and self._hand_edit(rec, view, now)):
                summary["skipped_locked"] += 1
                continue
            if info["channel_id"] in view.failed:
                summary["failed"] += 1
                summary["errors"].append(f"{info.get('name')}: could not read its channel from LSP")
                continue
            try:
                self.lsp.remove_event(event_id, force=True)
                self.state.forget(ref)
                summary["deleted"] += 1
                log.info("Deleted tool-created event %r (id=%s)", info.get("name"), event_id)
            except LspError as exc:
                summary["failed"] += 1
                summary["errors"].append(f"{info.get('name')}: {exc}")
                log.warning("Could not delete event %s: %s", event_id, exc)
        self.state.save()
        log.info("Cleanup complete: deleted=%d failed=%d skipped: started=%d locked=%d",
                 summary["deleted"], summary["failed"], summary["skipped_started"],
                 summary["skipped_locked"])
        return summary

    def delete_events(self, event_ids: list[str]) -> dict:
        """Delete these LSP events (one sheet event's bookings, from the
        Scheduled list) and stop tracking them. Runs even with dry run on:
        it's an explicit operator action, like cleanup."""
        summary = {"deleted": 0, "failed": 0, "errors": []}
        for event_id in event_ids:
            try:
                self.lsp.remove_event(event_id, force=True)
                self.state.forget_event(event_id)
                summary["deleted"] += 1
                log.info("Deleted event %s from LSP (Scheduled list)", event_id)
            except LspError as exc:
                summary["failed"] += 1
                summary["errors"].append(str(exc))
                log.warning("Could not delete event %s: %s", event_id, exc)
        self.state.save()
        return summary

    # -- helpers ------------------------------------------------------------

    def _resolve_channels(self, channels: Optional[list[dict]] = None) -> dict[str, list[tuple[str, str]]]:
        """PCR letter -> [(channel_id, channel_name)] for every LSP channel that
        matches it, looked up live so added/renamed channels are picked up."""
        if channels is None:
            channels = self.lsp.get_all_channels()
        resolved: dict[str, list[tuple[str, str]]] = {}
        for pcr, ref in self.cfg.pcr_channel_map.items():
            found = [(c["Id"], c.get("Name") or c["Id"]) for c in channels
                     if c.get("Id") and (c["Id"] == ref.channel_id or ref.matches(c.get("Name") or ""))]
            if ref.channel_id and not any(cid == ref.channel_id for cid, _ in found):
                log.warning("PCR %s: channel_id %s not found on server", pcr, ref.channel_id)
            if found:
                resolved[pcr] = sorted(found, key=lambda f: f[1].lower())
            else:
                log.warning("PCR %s: no LSP channels match %r", pcr, ref.match)
        return resolved

    def _within_window(self, ev: ScheduledEvent) -> bool:
        now = datetime.now(timezone.utc)
        start_utc = ev.start.astimezone(timezone.utc)
        if start_utc < now - timedelta(minutes=self.cfg.scheduling.past_grace_minutes):
            return False
        if start_utc > now + timedelta(days=self.cfg.scheduling.horizon_days):
            return False
        return True


_COUNTER = {CREATE: "created", UPDATE: "updated", DELETE: "deleted"}

_DUPLICATE = "another LSP event already has this name and start there; left as is"
_OWNED = "already in LSP for another sheet event this tool tracks; left as is"
_HAND_DIFFERS = "made by hand in LSP and differs from the sheet; left alone"
_GONE = "channel no longer in LSP; skipped"


def _note_overlaps(items: list[PlanItem], view: _LspView) -> None:
    """Say in Preview when an event a pass would create or move overlaps
    another one on the same channel once the pass is done (a doubleheader
    whose first game's record length runs into the second, a hand-made
    event...). Only a note: LSP decides whether a channel may hold both."""
    after: dict[str, dict] = defaultdict(dict)  # channel -> key -> (start, end, name)
    for cid, evs in view.by_channel.items():
        for e in evs:
            s, en = _parse_utc(e.get("Start")), _parse_utc(e.get("End"))
            if s and en and _status(e) not in ("canceled", "complete", "failed", "timeout"):
                after[cid][e.get("Id")] = (s, en, e.get("Name") or "")
    moving = []
    for n, item in enumerate(items):
        for t in item.targets:
            if t.status == DELETE:
                after[t.channel_id].pop(t.event_id, None)
            elif t.status in (CREATE, UPDATE):
                key = t.event_id or ("new", n)
                after[t.channel_id][key] = (item.event.start, item.event.end, item.lsp_name)
                moving.append((item, t, key))
    noted: dict[int, list[str]] = defaultdict(list)
    for item, t, key in moving:
        s, en, _ = after[t.channel_id][key]
        for other, (os_, oe, oname) in after[t.channel_id].items():
            if other != key and os_ < en and s < oe:
                what = (f"“{oname}” {os_.astimezone(LOCAL_TZ):%H:%M}–"
                        f"{oe.astimezone(LOCAL_TZ):%H:%M}")
                t.message += f"; overlaps {what}"
                if what not in noted[id(item)]:
                    noted[id(item)].append(what)
                    item.message += f"; overlaps {what} in LSP"
                break


def _summary(dry_run: bool) -> dict:
    return {"parsed": 0, "created": 0, "updated": 0, "deleted": 0, "skipped_existing": 0,
            "locked": 0, "not_in_sheet": 0, "skipped_window": 0, "no_channel": 0,
            "errors": 0, "dry_run": dry_run, "problems": [], "warnings": []}


def _event_id(result) -> Optional[str]:
    """The event id in an AddEvent reply (the Event, per the API docs)."""
    if isinstance(result, dict):
        return result.get("Id") or result.get("id") or result.get("EventId")
    if isinstance(result, str) and len(result.strip('"')) == 36:
        return result.strip('"')  # a bare id
    return None


def _describe(result) -> str:
    """A short description of an unexpected reply, for the log."""
    if isinstance(result, dict):
        return f"keys: {', '.join(sorted(result)) or 'none'}"
    return f"{type(result).__name__}: {str(result)[:120]}"


def _variables(e: dict):
    """(name, holder, is_workflow_variable) for each variable on an LSP event:
    its workflow (Vantage) variables -- Customization.Conditions, value in
    ConditionValue.Text -- then its workflow and label parameters (value in
    Text)."""
    cust = e.get("Customization") or {}
    for c in cust.get("Conditions") or []:
        yield (c or {}).get("Name") or "", c or {}, True
    params = [cust.get("Parameters") or []]
    params += [(label or {}).get("Parameters") or []
               for label in (e.get("Labels") or []) + (cust.get("Labels") or [])]
    for plist in params:
        for p in plist:
            yield (p or {}).get("Name") or "", p or {}, False


def _variable(e, variable: str) -> Optional[tuple[dict, bool]]:
    """(holder, is_workflow_variable) for the variable named `variable`
    (ignoring case and spacing) on an event."""
    want = " ".join((variable or "").split()).lower()
    if not want or not isinstance(e, dict):
        return None
    for name, holder, is_var in _variables(e):
        if " ".join(name.split()).lower() == want:
            return holder, is_var
    return None


def _variable_names(e: dict) -> list[str]:
    return [name for name, _, _ in _variables(e)]


def _texts(value) -> str:
    return " ".join(t for t in ((value or {}).get("Text") or []) if t)


def _variable_default(holder: dict, is_var: bool) -> str:
    box = (holder.get("ConditionValue") or {}) if is_var else holder
    return _texts(box.get("Default"))


def _variable_own(e: dict, variable: str) -> str:
    """An LSP event's own event-name variable value ("" if it has none, so
    it uses its default, or has no such variable)."""
    found = _variable(e, variable)
    if found is None:
        return ""
    holder, is_var = found
    own = _texts(holder.get("ConditionValue") if is_var else holder).strip()
    return "" if own == _variable_default(holder, is_var).strip() else own


def _variable_value(e: dict, variable: str) -> Optional[str]:
    """An LSP event's event-name variable value (its default when it has no
    value of its own); None if the event has no such variable."""
    found = _variable(e, variable)
    if found is None:
        return None
    holder, is_var = found
    own = _texts(holder.get("ConditionValue") if is_var else holder)
    return own or _variable_default(holder, is_var)


def _with_variable(e, variable: str, value: str) -> Optional[dict]:
    """PatchEvent fields (the event's Customization or its Labels, whichever
    holds the variable, copied whole) that set its event-name variable to
    `value`; None if the event has no such variable or it already holds
    `value`. `e` must be the event as LSP holds it now, read by id."""
    current = _variable_value(e, variable) if isinstance(e, dict) else None
    if current is None or current.strip() == value.strip():
        return None
    # Only the part of the event that holds the variable is sent back: LSP
    # replaces what it's given whole, so everything else in it (the other
    # workflow variables, the workflow's description) must be as LSP has it.
    for key in ("Customization", "Labels"):
        if e.get(key) and _variable({key: e[key]}, variable) is not None:
            fields = {key: copy.deepcopy(e[key])}
            break
    else:
        return None
    holder, is_var = _variable(fields, variable)
    if is_var:
        holder["ConditionValue"] = holder.get("ConditionValue") or {}
        holder = holder["ConditionValue"]
    holder["Text"] = [value]
    return fields


def _workflow_size(e: Optional[dict]) -> int:
    """How much workflow an LSP event carries: its workflow variables and
    parameters. 0 = none (its variable tab is greyed out in LSP)."""
    return sum(1 for _ in _variables(e)) if isinstance(e, dict) else 0


def _fresh_workflow(template: dict) -> dict:
    """PatchEvent fields giving an event `template`'s workflow (its
    Customization and Labels) with every value cleared, so the channel's
    defaults apply, as on a new event: no values from the template event
    (its crew, its own Event Name) carry over."""
    fields = {k: copy.deepcopy(template[k]) for k in ("Customization", "Labels")
              if template.get(k)}
    for _name, holder, is_var in _variables(fields):
        box = holder.get("ConditionValue") if is_var else holder
        if isinstance(box, dict) and "Text" in box:
            box["Text"] = []
    return fields


def _record_fields(item: PlanItem) -> dict:
    """What a record remembers about its sheet event (for matching and display)."""
    ev = item.event
    return {"tab": ev.source_tab, "column": ev.source_column, "name": ev.name,
            "date": ev.event_date, "occurrence": ev.occurrence, "same_day": ev.same_day,
            "sheet_start": ev.sheet_start, "pcr": ev.pcr,
            "lsp_name": item.lsp_name, "start": ev.start.isoformat(), "end": ev.end.isoformat()}


def _snapshot(e: dict) -> dict:
    return {f: e.get(f) for f in _SNAPSHOT_FIELDS}


def _written(item: PlanItem) -> dict:
    """The values the tool sends LSP for an event."""
    return {"Name": item.lsp_name, "Start": item.event.start.isoformat(),
            "End": item.event.end.isoformat()}


def _matches_written(e: dict, written: Optional[dict]) -> bool:
    """Whether an LSP event still holds what the tool last sent it."""
    if not written:
        return False
    start, end = _parse_utc(written.get("Start")), _parse_utc(written.get("End"))
    return (bool(start and end)
            and (e.get("Name") or "").strip() == (written.get("Name") or "").strip()
            and _same_instant(e.get("Start"), start) and _same_instant(e.get("End"), end))


def _locate(b: dict, cid: str, view: _LspView) -> Optional[tuple[str, dict]]:
    """(channel id, LSP event) for a booking: by its id, or, if LSP gave the
    event a new id, the event on its channel holding what the tool wrote,
    made by whoever made the original (an event someone deleted and made
    again by hand with the same values is theirs, not the tool's)."""
    hit = view.by_id.get(b.get("event_id"))
    if hit is not None:
        return hit
    w = b.get("written")
    start = _parse_utc((w or {}).get("Start"))
    if not start:
        return None
    creator = (b.get("snapshot") or {}).get("CreatedByDisplayName")
    for e in view.by_channel.get(cid, []):
        if _matches_written(e, w) and (not creator or not e.get("CreatedByDisplayName")
                                       or e["CreatedByDisplayName"] == creator):
            return cid, e
    return None


def _live_status(rec: dict, view: _LspView) -> str:
    """The status of one of the record's tool-made events that LSP has
    queued, started or finished ("" if none), whatever its times say."""
    for cid, b in (rec.get("bookings") or {}).items():
        if b.get("by_tool"):
            hit = _locate(b, cid, view)
            if hit and _status(hit[1]) in _ACTIVE:
                return _status(hit[1])
    return ""


def _kept_as_written(b: dict, cur: dict, name: str) -> bool:
    """Whether LSP holds the name the tool last wrote, as LSP stored it (it
    may trim, shorten or respace it), so it isn't re-sent every pass."""
    sent = ((b.get("written") or {}).get("Name") or "").strip()
    seen = ((b.get("snapshot") or {}).get("Name") or "").strip()
    return bool(sent) and sent == name.strip() and (cur.get("Name") or "").strip() == seen


def _booking(e: dict, channel_name: str, by_tool: bool) -> dict:
    return {"event_id": e.get("Id"), "channel_name": channel_name, "by_tool": by_tool,
            "created_at": None, "name": e.get("Name"), "snapshot": _snapshot(e)}


def _same_instant(value, dt: datetime) -> bool:
    """Whether an LSP timestamp is the same minute as `dt`. A timestamp with
    no offset is accepted as either UTC or local time (the TZ env var), since
    the LSP API docs don't say which it returns."""
    try:
        parsed = dateparser.isoparse(value)
    except (ValueError, TypeError):
        return False
    target = _minute_key(dt)
    if parsed.tzinfo:
        return _minute_key(parsed) == target
    return target in (_minute_key(parsed.replace(tzinfo=timezone.utc)), _minute_key(parsed.replace(tzinfo=LOCAL_TZ)))


def _lsp_time(value):
    """An LSP timestamp as a comparable instant (the raw text if unparseable).
    Both sides of a comparison come from LSP, so a missing offset is read the
    same way on each."""
    return _parse_utc(value) or value


def _same_lsp_time(a, b) -> bool:
    """Whether two LSP timestamps (from different LSP calls, which may not
    format them alike) are the same minute."""
    if a == b:
        return True
    try:
        parsed = dateparser.isoparse(b)
    except (ValueError, TypeError):
        return False
    if parsed.tzinfo:
        return _same_instant(a, parsed)
    return _same_instant(a, parsed.replace(tzinfo=timezone.utc)) or _same_instant(
        a, parsed.replace(tzinfo=LOCAL_TZ))


def _minute_key(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _parse_any(value) -> Optional[datetime]:
    try:
        return dateparser.isoparse(value)
    except (ValueError, TypeError):
        return None


def _parse_utc(value) -> Optional[datetime]:
    try:
        dt = dateparser.isoparse(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
