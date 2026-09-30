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
from typing import Optional

from dateutil import parser as dateparser

from .config import Config
from .lsp_client import LspClient, LspError
from .models import ScheduledEvent
from .runtime import LOCAL_TZ
from .sheets import SheetError, SheetReader
from .state import State

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

# The LSP fields a booking snapshot keeps; a change to any of them by someone
# other than this tool locks the event.
_SNAPSHOT_FIELDS = ("Name", "Start", "End", "ChannelId")

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
            "record_id": self.record_id,
            "locked": self.locked or bool(self.new_lock),
        }


class _LspView:
    """Every event on a set of channels, fetched once per pass."""

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

    def find(self, channel_id: str, name: str, start: datetime) -> Optional[dict]:
        """An event on the channel with this name and start minute."""
        want = name.strip().lower()
        for e in self.by_channel.get(channel_id, []):
            if (e.get("Name") or "").strip().lower() == want and _same_instant(e.get("Start"), start):
                return e
        return None


class Syncer:
    def __init__(self, cfg: Config, reader: Optional[SheetReader], lsp: LspClient, state: State,
                 stop: Optional[threading.Event] = None):
        # cfg may be an LspSettings and reader None for LSP-only use (channels,
        # scheduled view, cleanup) before the sheet side is configured.
        self.cfg = cfg
        self.reader = reader
        self.lsp = lsp
        self.state = state
        # Set on shutdown: a pass stops between LSP writes (state saved).
        self.stop = stop or threading.Event()
        self.sheet_errors: list[str] = []

    # -- planning (read-only) ----------------------------------------------

    def plan(self) -> list[PlanItem]:
        events = self.reader.read_events()
        self.sheet_errors = list(getattr(self.reader, "errors", None) or [])
        if not events and not self.state.events:
            return []

        channels = self.lsp.get_all_channels()
        by_pcr = self._resolve_channels(channels)
        names = {c["Id"]: c.get("Name") or c["Id"] for c in channels if c.get("Id")}
        wanted = {cid for found in by_pcr.values() for cid, _ in found}
        wanted |= {cid for rec in self.state.events.values() for cid in rec.get("bookings", {})}
        view = _LspView(self.lsp, wanted)
        self._view = view  # _execute rebaselines snapshots from it

        now = datetime.now(timezone.utc)
        matches = self._match(events, now)
        planned_keys: set[tuple[str, str, str]] = set()
        items: list[PlanItem] = []
        for i, ev in enumerate(events):
            rid = matches.get(i)
            if rid is None:
                items.append(self._plan_new(ev, by_pcr, view, planned_keys))
            else:
                items.append(self._plan_tracked(ev, rid, by_pcr, names, view, now))

        matched = set(matches.values())
        for rid, rec in self.state.events.items():
            if rid not in matched:
                item = self._plan_not_in_sheet(rid, rec, names, view, now)
                if item:
                    items.append(item)
        return items

    def _match(self, events: list[ScheduledEvent], now: datetime) -> dict[int, str]:
        """Pair sheet events with tracked records: index -> record id.

        1. same tab + date + name + occurrence (a time or PCR change);
        2. same tab + name, when exactly one unmatched event and one unmatched
           record share it (a date change);
        3. same tab + column + date, likewise (a name change).
        Steps 2-3 only consider records that haven't started, so a finished
        game never swallows a later rematch.
        """
        out: dict[int, str] = {}
        used: set[str] = set()
        exact: dict[tuple, str] = {}
        for rid, rec in self.state.events.items():
            exact.setdefault((rec["tab"], rec["date"], rec["name"].strip().lower(),
                              rec.get("occurrence", 0)), rid)
        for i, ev in enumerate(events):
            rid = exact.get((ev.source_tab, *ev.override_key()))
            if rid and rid not in used:
                out[i] = rid
                used.add(rid)

        loose = [
            (lambda ev: (ev.source_tab, ev.name.strip().lower()),
             lambda rec: (rec["tab"], rec["name"].strip().lower())),
            (lambda ev: (ev.source_tab, ev.source_column, ev.event_date),
             lambda rec: (rec["tab"], rec.get("column"), rec["date"])),
        ]
        for ev_key, rec_key in loose:
            ev_groups: dict[tuple, list[int]] = defaultdict(list)
            for i, ev in enumerate(events):
                if i not in out:
                    ev_groups[ev_key(ev)].append(i)
            rec_groups: dict[tuple, list[str]] = defaultdict(list)
            for rid, rec in self.state.events.items():
                start = _parse_utc(rec.get("start"))
                if rid not in used and start and start > now:
                    rec_groups[rec_key(rec)].append(rid)
            for k, idxs in ev_groups.items():
                rids = rec_groups.get(k, [])
                if len(idxs) == 1 and len(rids) == 1:
                    out[idxs[0]] = rids[0]
                    used.add(rids[0])
        return out

    def _plan_new(self, ev, by_pcr, view: _LspView, planned_keys) -> PlanItem:
        """A sheet event the tool isn't tracking yet."""
        name = ev.lsp_name(self.cfg.scheduling.event_name_prefix)
        chans = by_pcr.get(ev.pcr) or []
        if not chans:
            return PlanItem(ev, name, NO_CHANNEL,
                            message=f"No LSP channels match PCR {ev.pcr or '(none)'}")
        if not self._within_window(ev):
            return PlanItem(ev, name, OUT_OF_WINDOW,
                            [Target(cid, cname, OUT_OF_WINDOW) for cid, cname in chans],
                            message="Start is outside the active window")

        key = (name.strip().lower(), _minute_key(ev.start))
        targets = []
        for cid, cname in chans:
            if (cid, *key) in planned_keys:
                targets.append(Target(cid, cname, EXISTS, "Same event earlier in the sheet"))
                continue
            if cid in view.failed:
                targets.append(Target(cid, cname, ERROR, "Could not read this channel from LSP"))
                continue
            found = view.find(cid, name, ev.start)
            if found:
                targets.append(Target(cid, cname, EXISTS, "Already in LSP (made by hand; left alone)",
                                      event_id=found.get("Id"),
                                      adopt=_booking(found, cname, by_tool=False)))
                continue
            planned_keys.add((cid, *key))
            targets.append(Target(cid, cname, CREATE, "Will be created"))

        if any(t.status == ERROR for t in targets):
            return PlanItem(ev, name, ERROR, targets, message="Could not read LSP; skipped this pass")
        to_create = sum(t.status == CREATE for t in targets)
        if to_create:
            msg = (f"Will be created on all {len(targets)} channels" if to_create == len(targets)
                   else f"Will be created on {to_create} of {len(targets)} channels")
            return PlanItem(ev, name, CREATE, targets, message=msg)
        return PlanItem(ev, name, EXISTS, targets, message=f"Already on all {len(targets)} channels")

    def _plan_tracked(self, ev, rid, by_pcr, names, view: _LspView, now) -> PlanItem:
        """A sheet event the tool already scheduled: bring LSP in line with it."""
        rec = self.state.events[rid]
        name = ev.lsp_name(self.cfg.scheduling.event_name_prefix)
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
        started = _parse_utc(rec.get("start"))
        if started and started <= now:
            return PlanItem(ev, name, EXISTS, as_is(EXISTS, "Started"), record_id=rid,
                            message="Started; sheet changes are no longer applied")
        edit = self._hand_edit(rec, view, now)
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
        targets, wanted = [], set()
        for cid, cn in chans:
            wanted.add(cid)
            b = bookings.get(cid)
            if b is None:
                targets.append(Target(cid, cn, CREATE, "Will be created"))
            elif not b.get("by_tool"):
                targets.append(Target(cid, cn, EXISTS, "Made by hand in LSP; left alone",
                                      event_id=b.get("event_id")))
            else:
                # Untouched by hand (checked above), so LSP's current values
                # are what the tool last wrote; the snapshot is the fallback.
                hit = _locate(b, cid, view)
                cur = hit[1] if hit else (b.get("snapshot") or {})
                diffs = []
                if (cur.get("Name") or "").strip() != name.strip():
                    diffs.append("name")
                if not _same_instant(cur.get("Start"), ev.start):
                    diffs.append("start")
                if not _same_instant(cur.get("End"), ev.end):
                    diffs.append("end")
                var = self.cfg.lsp.event_name_variable
                if (hit and var and b.get("variable_sent") != name
                        and _with_variable(hit[1], var, name) is not None):
                    diffs.append(var)
                targets.append(Target(cid, cn, UPDATE if diffs else EXISTS,
                                      f"Will update {', '.join(diffs)}" if diffs else "Up to date",
                                      event_id=hit[1].get("Id") if hit else b.get("event_id")))
        for cid, b in bookings.items():
            if cid in wanted or not b.get("by_tool"):
                continue
            # The PCR the booking was made for (older bookings: the record's).
            if (b.get("pcr") or rec.get("pcr") or "") != (ev.pcr or ""):
                targets.append(Target(cid, cname(cid), DELETE,
                                      "PCR changed; will be removed from this channel",
                                      event_id=b.get("event_id")))
            else:
                # Same PCR, but the channel no longer matches it (renamed in
                # LSP, or the PCR's channel match was edited). Not a reason
                # to delete a booking; an engineer can remove it in LSP.
                targets.append(Target(cid, cname(cid), EXISTS,
                                      f"No longer matches PCR {ev.pcr}; left in LSP",
                                      event_id=b.get("event_id")))

        counts = defaultdict(int)
        for t in targets:
            counts[t.status] += 1
        if not (counts[CREATE] or counts[UPDATE] or counts[DELETE]):
            return PlanItem(ev, name, EXISTS, targets, record_id=rid,
                            message=f"Up to date on all {len(targets)} channels")
        parts = [f"{verb} on {counts[s]}" for s, verb in
                 ((UPDATE, "update"), (CREATE, "create"), (DELETE, "remove")) if counts[s]]
        return PlanItem(ev, name, UPDATE, targets, record_id=rid,
                        message="Sheet changed: will " + ", ".join(parts) + " channel(s)")

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
        edit = ("" if rec.get("locked") or start <= now
                else (self._hand_edit(rec, view, now) or ""))
        msg = "No longer in the sheet; left in LSP (delete it there if it's cancelled)"
        if rec.get("locked") or edit:
            msg += f". Locked: {rec.get('lock_reason') or edit}"
        return PlanItem(ev, rec.get("lsp_name") or rec["name"], NOT_IN_SHEET, targets, msg,
                        record_id=rid, locked=bool(rec.get("locked")), new_lock=edit)

    def _hand_edit(self, rec, view: _LspView, now) -> Optional[str]:
        """Why this record's LSP events no longer match what the tool last
        wrote ("" = untouched), or None if LSP couldn't be read.

        A booking is untouched if LSP matches its snapshot *or* the values the
        tool last sent: the re-read right after a write can still return the
        old values, which would otherwise look like a hand edit next pass."""
        for cid, b in rec.get("bookings", {}).items():
            if not b.get("by_tool"):
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
                return f"{label}: deleted or moved in LSP"
            found_cid, e = hit
            if found_cid != cid:
                return f"{label}: moved to another channel in LSP"
            if snap is None or _matches_written(e, b.get("written")):
                continue  # no baseline yet (taken next pass), or as the tool left it
            if (e.get("Name") or "").strip() != (snap.get("Name") or "").strip():
                return f"{label}: name changed in LSP"
            for f in ("Start", "End"):
                if _lsp_time(e.get(f)) != _lsp_time(snap.get(f)):
                    return f"{label}: {f.lower()} changed in LSP"
        return ""

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
        try:
            self._execute(items, summary, live=not self.cfg.runtime.dry_run)
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
        want = (event_date, event_name.strip().lower(), occurrence)
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
        (updating the edited bookings back and re-creating deleted ones).
        """
        rec = self.state.record(record_id)
        if rec is None:
            raise LspError("That event isn't tracked any more — refresh the preview")
        view = _LspView(self.lsp, set(rec.get("bookings", {})))
        if view.failed:
            raise LspError("Could not read LSP; try again")
        for cid, b in list(rec["bookings"].items()):
            if not b.get("by_tool"):
                continue
            hit = _locate(b, cid, view)
            if hit and hit[0] == cid:
                b.update(event_id=hit[1].get("Id"), snapshot=_snapshot(hit[1]))
            else:
                del rec["bookings"][cid]
        rec.update(locked=False, lock_reason="", locked_at=None)
        if not rec["bookings"]:
            self.state.events.pop(record_id, None)
        self.state.save()
        log.info("Unlocked %r; the sheet is applied to it again", rec.get("lsp_name") or rec["name"])
        return {"name": rec.get("lsp_name") or rec["name"]}

    def _execute(self, items: list[PlanItem], summary: dict, live: bool, force: bool = False) -> None:
        """Apply `items` to LSP (or just log them when not `live`), recording
        the results in state and counting into `summary`. `force` lets the
        LSP client write even when it was built read-only."""
        touched: list[tuple[str, str]] = []  # (record id, channel id) to rebaseline
        now = _now_iso()
        for item in items:
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
                log.warning("%s: %s", item.lsp_name, item.message)
                continue
            if item.status == NOT_IN_SHEET:
                summary["not_in_sheet"] += 1
                continue
            if item.status == ERROR:
                summary["errors"] += 1
                continue

            rid = item.record_id
            if rid and not (item.new_lock or item.locked):
                self._refresh_baselines(rid)
            if item.status == LOCKED:
                summary["locked"] += 1
                continue

            for t in item.targets:
                if t.status == EXISTS:
                    summary["skipped_existing"] += 1
                    if t.adopt is not None:
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
                    continue
                summary[_COUNTER[t.status]] += 1
                self._record_write()

            if live and rid:
                self.state.events[rid].update(_record_fields(item))
        self._rebaseline(touched)

    def _record_write(self) -> None:
        """Save state right after each LSP write, so a restart mid-pass
        can't leave events in LSP that the tool has no record of (the next
        pass would take them for hand-made ones and stop updating them).
        The record's own fields are only refreshed once the whole item is
        done, so an interrupted PCR move still looks like one next pass."""
        self.state.save()

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
            view = getattr(self, "_view", None)
            hit = view.by_id.get(t.event_id) if view else None
            if hit:
                self._fill_variable(item, t, t.event_id, hit[1], rid, force)
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
        if created:
            self._fill_variable(item, t, event_id, created, rid, force)
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

    def _fill_variable(self, item: PlanItem, t: Target, event_id: str, current,
                       rid: str, force: bool) -> None:
        """Set the event's event-name variable to its name, if the event has
        that variable and it holds something else (e.g. its default), then
        read the event back to check LSP kept it. Tried once per name, so a
        value LSP won't keep isn't re-sent every pass; a failure is logged
        and never undoes the create / update."""
        var = self.cfg.lsp.event_name_variable
        fields = _with_variable(current, var, item.lsp_name) if var else None
        if fields is None:
            return
        self.state.events[rid]["bookings"][t.channel_id]["variable_sent"] = item.lsp_name
        ev = item.event
        try:
            self.lsp.patch_event(event_id, item.lsp_name, ev.start, ev.end,
                                 force=force, extra=fields)
        except LspError as exc:
            log.warning("Could not set %r on %r on %s: %s", var, item.lsp_name, t.channel_name, exc)
            return
        holds = self._read_variable(event_id, var)
        if holds == item.lsp_name:
            log.info("Set %r = %r on %s", var, item.lsp_name, t.channel_name)
        else:
            log.warning("LSP didn't keep %r = %r on %s: it holds %r",
                        var, item.lsp_name, t.channel_name, holds)

    def _read_variable(self, event_id: str, variable: str) -> Optional[str]:
        """The event-name variable's value on an LSP event, read fresh."""
        try:
            return _variable_value(self.lsp.get_event(event_id) or {}, variable)
        except LspError as exc:
            log.warning("Could not read event %s back from LSP: %s", event_id, exc)
            return None

    def _refresh_baselines(self, rid: str) -> None:
        """For an untouched record's bookings: follow an event LSP gave a new
        id, and re-snapshot one whose last re-read failed or came back stale
        (LSP now shows what the tool wrote)."""
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
            if b.get("snapshot") is None or _matches_written(e, b.get("written")):
                b["snapshot"] = _snapshot(e)

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
        unlock one to include it.
        """
        summary = {"deleted": 0, "failed": 0, "errors": [], "skipped_locked": 0,
                   "skipped_started": 0}
        now = datetime.now(timezone.utc)
        for ref, info in self.state.tool_created():
            event_id = info.get("event_id")
            start = _parse_utc(info.get("start"))
            if start is None or start <= now:
                summary["skipped_started"] += 1
                continue
            if info.get("locked"):
                summary["skipped_locked"] += 1
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


def _summary(dry_run: bool) -> dict:
    return {"parsed": 0, "created": 0, "updated": 0, "deleted": 0, "skipped_existing": 0,
            "locked": 0, "not_in_sheet": 0, "skipped_window": 0, "no_channel": 0,
            "errors": 0, "dry_run": dry_run, "problems": []}


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
    """PatchEvent fields (Customization and/or Labels, copied from the event)
    that set its event-name variable to `value`; None if the event has no
    such variable or it already holds `value`."""
    current = _variable_value(e, variable) if isinstance(e, dict) else None
    if current is None or current.strip() == value.strip():
        return None
    fields = {k: copy.deepcopy(e[k]) for k in ("Customization", "Labels") if e.get(k)}
    holder, is_var = _variable(fields, variable)
    if is_var:
        holder["ConditionValue"] = holder.get("ConditionValue") or {}
        holder = holder["ConditionValue"]
    holder["Text"] = [value]
    return fields


def _record_fields(item: PlanItem) -> dict:
    """What a record remembers about its sheet event (for matching and display)."""
    ev = item.event
    return {"tab": ev.source_tab, "column": ev.source_column, "name": ev.name,
            "date": ev.event_date, "occurrence": ev.occurrence, "pcr": ev.pcr,
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
    event a new id, the event on its channel holding what the tool wrote."""
    hit = view.by_id.get(b.get("event_id"))
    if hit is not None:
        return hit
    w = b.get("written")
    start = _parse_utc((w or {}).get("Start"))
    if not start:
        return None
    for e in view.by_channel.get(cid, []):
        if _matches_written(e, w):
            return cid, e
    return None


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


def _minute_key(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _parse_utc(value) -> Optional[datetime]:
    try:
        dt = dateparser.isoparse(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
