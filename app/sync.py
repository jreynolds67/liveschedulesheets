"""Orchestrate a sync: sheet -> resolve channels -> match tracked events -> plan -> apply.

Each sheet event the tool schedules is tracked as a *record* in `State`, with
one *booking* per LSP channel. A pass keeps LSP in step with the sheet:

- a new sheet event is created on every channel its PCR maps to;
- a tracked event whose name, time, date or PCR changed in the sheet (or whose
  lead-in / safety cap changed) is updated in place, and moved between
  channels when its PCR changes;
- a tracked event that someone changed in LSP by hand (edited, deleted, or
  moved to another channel) is *locked*: the tool never changes it again until
  an engineer unlocks it in the UI.

`plan()` is read-only and classifies every event; `run_once()` executes the
plan. The web UI uses `plan()` for its preview so what you see is exactly what
a real pass would do.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from dateutil import parser as dateparser

from .config import Config
from .lsp_client import LspClient, LspError
from .models import ScheduledEvent
from .sheets import SheetReader
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


@dataclass
class Target:
    """One LSP channel an event goes on, and what a pass would do there."""
    channel_id: str
    channel_name: str
    status: str
    message: str = ""
    event_id: Optional[str] = None
    # For a new event: a booking to record without creating anything (a hand-
    # made LSP event already there, or one adopted from the legacy state).
    adopt: Optional[dict] = None
    legacy_key: Optional[str] = None


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
    def __init__(self, cfg: Config, reader: Optional[SheetReader], lsp: LspClient, state: State):
        # cfg may be an LspSettings and reader None for LSP-only use (channels,
        # scheduled view, cleanup) before the sheet side is configured.
        self.cfg = cfg
        self.reader = reader
        self.lsp = lsp
        self.state = state

    # -- planning (read-only) ----------------------------------------------

    def plan(self) -> list[PlanItem]:
        events = self.reader.read_events()
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
        targets, lock = [], ""
        for cid, cname in chans:
            legacy_key = ev.dedup_key(cid)
            legacy = self.state.created.get(legacy_key)
            if isinstance(legacy, dict):
                t, why = self._adopt_legacy(legacy, legacy_key, cid, cname, name, ev, view)
                targets.append(t)
                lock = lock or why
                continue
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

        if lock:
            return PlanItem(ev, name, LOCKED, targets, new_lock=lock,
                            message=f"Locked: {lock}")
        if any(t.status == ERROR for t in targets):
            return PlanItem(ev, name, ERROR, targets, message="Could not read LSP; skipped this pass")
        to_create = sum(t.status == CREATE for t in targets)
        if to_create:
            msg = (f"Will be created on all {len(targets)} channels" if to_create == len(targets)
                   else f"Will be created on {to_create} of {len(targets)} channels")
            return PlanItem(ev, name, CREATE, targets, message=msg)
        return PlanItem(ev, name, EXISTS, targets, message=f"Already on all {len(targets)} channels")

    def _adopt_legacy(self, legacy, legacy_key, cid, cname, name, ev, view) -> tuple[Target, str]:
        """A booking from the old state format: track it as a record booking.
        Returns the target and, if it was changed in LSP, a lock reason."""
        if legacy.get("existed") and not legacy.get("created_by_tool"):
            found = view.find(cid, name, ev.start)
            return Target(cid, cname, EXISTS, "Already in LSP (made by hand; left alone)",
                          adopt=_booking(found, cname, by_tool=False) if found
                          else {"by_tool": False, "channel_name": cname},
                          legacy_key=legacy_key), ""
        eid = legacy.get("event_id")
        hit = view.by_id.get(eid) if eid else None
        if hit is None or hit[0] != cid:
            b = {"event_id": eid, "channel_name": cname, "by_tool": True,
                 "created_at": legacy.get("created_at"), "name": legacy.get("name")}
            return Target(cid, cname, LOCKED, "Deleted or moved in LSP", event_id=eid,
                          adopt=b, legacy_key=legacy_key), f"{cname}: deleted or moved in LSP"
        e = hit[1]
        b = _booking(e, cname, by_tool=True, created_at=legacy.get("created_at"))
        # The legacy key embeds the sheet name and start, so any difference
        # here was made in LSP. (End isn't compared: the cap may have changed.)
        why = ""
        if (e.get("Name") or "").strip() != name.strip():
            why = f"{cname}: name changed in LSP"
        elif not _same_instant(e.get("Start"), ev.start):
            why = f"{cname}: start changed in LSP"
        return Target(cid, cname, LOCKED if why else EXISTS, why or "Already created",
                      event_id=eid, adopt=b, legacy_key=legacy_key), why

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
        edit = self._hand_edit(rec, view, now)
        if edit is None:
            return PlanItem(ev, name, ERROR, as_is(ERROR, "Could not read LSP"), record_id=rid,
                            message="Could not read LSP; skipped this pass")
        if edit:
            return PlanItem(ev, name, LOCKED, as_is(LOCKED, edit), record_id=rid, new_lock=edit,
                            message=f"Will lock: {edit}. Sheet changes won't be applied.")
        started = _parse_utc(rec.get("start"))
        if started and started <= now:
            return PlanItem(ev, name, EXISTS, as_is(EXISTS, "Started"), record_id=rid,
                            message="Started; sheet changes are no longer applied")

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
                targets.append(Target(cid, cn, UPDATE if diffs else EXISTS,
                                      f"Will update {', '.join(diffs)}" if diffs else "Up to date",
                                      event_id=hit[1].get("Id") if hit else b.get("event_id")))
        for cid, b in bookings.items():
            if cid not in wanted and b.get("by_tool"):
                targets.append(Target(cid, cname(cid), DELETE,
                                      "PCR changed; will be removed from this channel",
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
        edit = "" if rec.get("locked") else (self._hand_edit(rec, view, now) or "")
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
        except LspError:
            log.exception("Could not build plan; aborting pass")
            summary["errors"] += 1
            return summary

        summary["parsed"] = sum(i.status != NOT_IN_SHEET for i in items)
        self._execute(items, summary, live=not self.cfg.runtime.dry_run)
        self.state.save()
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
        self._execute([item], summary, live=True, force=True)
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
            if rid is None and item.new_lock:
                # A legacy booking changed in LSP: track it, locked.
                rid = self._new_record(item)
                self.state.lock(rid, item.new_lock, now)
                log.warning("Locked %r: %s", item.lsp_name, item.new_lock)
            if item.status == LOCKED:
                summary["locked"] += 1
                for t in item.targets:
                    if t.adopt is not None and rid:
                        self.state.events[rid]["bookings"][t.channel_id] = t.adopt
                    if t.legacy_key:
                        self.state.unmark(t.legacy_key)
                continue

            for t in item.targets:
                if t.status == EXISTS:
                    summary["skipped_existing"] += 1
                    if t.adopt is not None:
                        rid = rid or self._new_record(item)
                        self.state.events[rid]["bookings"][t.channel_id] = t.adopt
                        if t.adopt.get("by_tool") and not t.adopt.get("snapshot"):
                            touched.append((rid, t.channel_id))
                    if t.legacy_key:
                        self.state.unmark(t.legacy_key)
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
                    summary["errors"] += 1
                    continue
                summary[_COUNTER[t.status]] += 1

            if live and rid:
                self.state.events[rid].update(_record_fields(item))
        self._rebaseline(touched)

    def _apply(self, item: PlanItem, t: Target, rid, force, now, touched) -> Optional[str]:
        """Carry out one CREATE / UPDATE / DELETE target; returns the record id."""
        ev = item.event
        if t.status == CREATE:
            result = self.lsp.add_event(ev, t.channel_id, item.lsp_name, force=force)
            event_id = result.get("Id") if isinstance(result, dict) else None
            rid = rid or self._new_record(item)
            self.state.events[rid]["bookings"][t.channel_id] = {
                "event_id": event_id, "channel_name": t.channel_name, "by_tool": True,
                "created_at": now, "name": item.lsp_name,
                "snapshot": _snapshot(result) if isinstance(result, dict) else None,
                "written": _written(item),
            }
            touched.append((rid, t.channel_id))
            log.info("Created %r on %s (PCR %s) @ %s (id=%s)", item.lsp_name, t.channel_name,
                     ev.pcr, ev.start.isoformat(), event_id)
        elif t.status == UPDATE:
            self.lsp.patch_event(t.event_id, item.lsp_name, ev.start, ev.end, force=force)
            b = self.state.events[rid]["bookings"][t.channel_id]
            b.update(event_id=t.event_id, name=item.lsp_name, snapshot=None,
                     written=_written(item), updated_at=now)
            touched.append((rid, t.channel_id))
            log.info("Updated %r on %s (%s) @ %s", item.lsp_name, t.channel_name,
                     t.message, ev.start.isoformat())
        elif t.status == DELETE:
            self.lsp.remove_event(t.event_id, force=force)
            self.state.events[rid]["bookings"].pop(t.channel_id, None)
            log.info("Removed %r from %s (PCR now %s)", item.lsp_name, t.channel_name, ev.pcr)
        return rid

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

    # -- cleanup (testing) --------------------------------------------------

    def created_events(self) -> list[dict]:
        """The events this tool created (from local state), for display."""
        out = []
        for _ref, info in self.state.tool_created():
            out.append({
                "name": info.get("name"),
                "pcr": info.get("pcr"),
                "source_tab": info.get("source_tab"),
                "channel_id": info.get("channel_id"),
                "event_id": info.get("event_id"),
                "created_at": info.get("created_at"),
            })
        out.sort(key=lambda e: e.get("created_at") or "")
        return out

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
                    "by_tool": info is not None,
                    "locked": bool(info and info.get("locked")),
                })
        events.sort(key=lambda ev: _parse_utc(ev["start"]) or cutoff)

        # Tool-created events not found on any mapped channel (deleted or
        # moved by hand in LSP, or their PCR is no longer mapped).
        missing = []
        for event_id, info in tool_ids.items():
            if event_id in all_ids:
                continue
            missing.append({"id": event_id, "name": info.get("name"),
                            "pcr": info.get("pcr"), "source_tab": info.get("source_tab")})
        return {"events": events, "missing": missing, "channels": len(pcrs_by_channel)}

    def delete_created(self) -> dict:
        """Delete from LSP every event this tool created, then forget them.

        Only touches events tagged as tool-created in local state, so events
        already present in LSP (or made by hand) are never removed. Runs even
        with dry run on, so events sent from Preview for testing can be
        cleaned up without taking the loop live. Locked events (changed by
        hand in LSP) are left alone too; unlock one to include it.
        """
        summary = {"deleted": 0, "failed": 0, "errors": [], "skipped_locked": 0}
        for ref, info in self.state.tool_created():
            event_id = info.get("event_id")
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
        log.info("Cleanup complete: deleted=%d failed=%d", summary["deleted"], summary["failed"])
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
            "errors": 0, "dry_run": dry_run}


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


def _booking(e: dict, channel_name: str, by_tool: bool, created_at=None) -> dict:
    return {"event_id": e.get("Id"), "channel_name": channel_name, "by_tool": by_tool,
            "created_at": created_at, "name": e.get("Name"), "snapshot": _snapshot(e)}


def _same_instant(value, dt: datetime) -> bool:
    """Whether an LSP timestamp is the same minute as `dt`. A timestamp with
    no offset is accepted as either UTC or this server's local time, since
    the LSP API docs don't say which it returns."""
    try:
        parsed = dateparser.isoparse(value)
    except (ValueError, TypeError):
        return False
    target = _minute_key(dt)
    if parsed.tzinfo:
        return _minute_key(parsed) == target
    return target in (_minute_key(parsed.replace(tzinfo=timezone.utc)), _minute_key(parsed.astimezone()))


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
