"""Orchestrate a sync: sheet -> resolve channels -> dedup -> plan -> create.

`plan()` is read-only and classifies every parsed event; `run_once()` executes
the plan (creating the ones marked CREATE). The web UI uses `plan()` for its
preview so what you see is exactly what a real pass would do.
"""
from __future__ import annotations

import logging
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

# Plan statuses
CREATE = "create"
EXISTS = "exists"
OUT_OF_WINDOW = "out_of_window"
NO_CHANNEL = "no_channel"


@dataclass
class Target:
    """One LSP channel an event goes on, and what a pass would do there."""
    channel_id: str
    channel_name: str
    status: str              # CREATE or EXISTS
    message: str = ""


@dataclass
class PlanItem:
    """One sheet event. Its control room maps to every matching LSP channel;
    `targets` says what happens on each. `status` summarizes them."""
    event: ScheduledEvent
    lsp_name: str
    status: str
    targets: list[Target] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.lsp_name,
            "event_name": self.event.name,
            "pcr": self.event.pcr,
            "start": self.event.start.isoformat(),
            "end": self.event.end.isoformat(),
            "status": self.status,
            "message": self.message,
            "channels": [{"id": t.channel_id, "name": t.channel_name, "status": t.status}
                         for t in self.targets],
            "source_tab": self.event.source_tab,
            "source_column": self.event.source_column,
            "event_date": self.event.event_date,
            "occurrence": self.event.occurrence,
        }


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
        if not events:
            return []

        channels_by_pcr = self._resolve_channels()
        existing_cache: dict[str, set[tuple[str, str]]] = {}
        planned_keys: set[tuple[str, str, str]] = set()
        items: list[PlanItem] = []

        for ev in events:
            name = ev.lsp_name(self.cfg.scheduling.event_name_prefix)
            channels = channels_by_pcr.get(ev.pcr) or []
            if not channels:
                items.append(PlanItem(ev, name, NO_CHANNEL,
                                      message=f"No LSP channels match PCR {ev.pcr or '(none)'}"))
                continue

            if not self._within_window(ev):
                items.append(PlanItem(ev, name, OUT_OF_WINDOW,
                                      [Target(cid, cname, OUT_OF_WINDOW) for cid, cname in channels],
                                      message="Start is outside the active window"))
                continue

            key = (name.strip().lower(), _minute_key(ev.start))
            targets = []
            for cid, cname in channels:
                if self.state.has(ev.dedup_key(cid)) or (cid, *key) in planned_keys:
                    targets.append(Target(cid, cname, EXISTS, "Already created"))
                    continue
                if cid not in existing_cache:
                    existing_cache[cid] = self._existing_keys(cid)
                if key in existing_cache[cid]:
                    targets.append(Target(cid, cname, EXISTS, "Already exists in LSP"))
                    continue
                planned_keys.add((cid, *key))
                targets.append(Target(cid, cname, CREATE, "Will be created"))

            to_create = sum(t.status == CREATE for t in targets)
            if to_create:
                msg = (f"Will be created on all {len(targets)} channels" if to_create == len(targets)
                       else f"Will be created on {to_create} of {len(targets)} channels")
                items.append(PlanItem(ev, name, CREATE, targets, message=msg))
            else:
                items.append(PlanItem(ev, name, EXISTS, targets,
                                      message=f"Already on all {len(targets)} channels"))
        return items

    # -- execution ----------------------------------------------------------

    def run_once(self) -> dict:
        """Execute the plan. Counts are per channel, except `parsed` / window /
        no-channel, which count sheet events."""
        summary = {"parsed": 0, "created": 0, "skipped_existing": 0,
                   "skipped_window": 0, "no_channel": 0, "errors": 0}
        try:
            items = self.plan()
        except LspError:
            log.exception("Could not build plan; aborting pass")
            summary["errors"] += 1
            return summary

        summary["parsed"] = len(items)
        for item in items:
            if item.status == OUT_OF_WINDOW:
                summary["skipped_window"] += 1
                continue
            if item.status == NO_CHANNEL:
                summary["no_channel"] += 1
                log.warning("%s", item.message)
                continue

            for t in item.targets:
                key = item.event.dedup_key(t.channel_id)
                if t.status == EXISTS:
                    summary["skipped_existing"] += 1
                    if not self.state.has(key):
                        self.state.mark(key, {"name": item.lsp_name, "channel_id": t.channel_id,
                                              "existed": True})
                    continue

                # t.status == CREATE
                if self.cfg.runtime.dry_run:
                    log.info("[DRY RUN] would create %r on %s (PCR %s) %s -> %s",
                             item.lsp_name, t.channel_name, item.event.pcr,
                             item.event.start.isoformat(), item.event.end.isoformat())
                    summary["created"] += 1
                    continue
                try:
                    result = self.lsp.add_event(item.event, t.channel_id, item.lsp_name)
                except LspError:
                    log.exception("Failed to create event %r on %s", item.lsp_name, t.channel_name)
                    summary["errors"] += 1
                    continue
                event_id = result.get("Id") if isinstance(result, dict) else None
                log.info("Created %r on %s (PCR %s) @ %s (id=%s)", item.lsp_name, t.channel_name,
                         item.event.pcr, item.event.start.isoformat(), event_id)
                self.state.mark(key, {"name": item.lsp_name, "channel_id": t.channel_id,
                                      "channel_name": t.channel_name,
                                      "event_id": event_id, "created_at": _now_iso(),
                                      "created_by_tool": True,
                                      "pcr": item.event.pcr, "source_tab": item.event.source_tab})
                summary["created"] += 1

        self.state.save()
        log.info(
            "Pass complete: parsed=%(parsed)d created=%(created)d existing=%(skipped_existing)d "
            "out-of-window=%(skipped_window)d no-channel=%(no_channel)d errors=%(errors)d",
            summary,
        )
        return summary

    # -- cleanup (testing) --------------------------------------------------

    def created_events(self) -> list[dict]:
        """The events this tool created (from local state), for display."""
        out = []
        for _key, info in self.state.tool_created():
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
        whether this tool created it, plus any tool-created events that are no
        longer found in LSP (deleted or moved by hand).
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
                events.append({
                    "id": e.get("Id"),
                    "name": e.get("Name"),
                    "pcr": "/".join(sorted(pcrs)),
                    "channel": names[channel_id],
                    "start": e.get("Start"),
                    "end": e.get("End"),
                    "status": e.get("Status"),
                    "created_by": e.get("CreatedByDisplayName"),
                    "by_tool": e.get("Id") in tool_ids,
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
        already present in LSP (or made by hand) are never removed.
        """
        summary = {"deleted": 0, "failed": 0, "errors": []}
        for key, info in self.state.tool_created():
            event_id = info.get("event_id")
            try:
                self.lsp.remove_event(event_id)
                self.state.unmark(key)
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

    def _existing_keys(self, channel_id: str) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        try:
            for e in self.lsp.get_events_for_channel(channel_id):
                name = (e.get("Name") or "").strip().lower()
                start = e.get("Start")
                if not start:
                    continue
                try:
                    dt = dateparser.isoparse(start)
                except (ValueError, TypeError):
                    continue
                keys.add((name, _minute_key(dt)))
        except LspError:
            log.exception("Could not list existing events for channel %s", channel_id)
        return keys

    def _within_window(self, ev: ScheduledEvent) -> bool:
        now = datetime.now(timezone.utc)
        start_utc = ev.start.astimezone(timezone.utc)
        if start_utc < now - timedelta(minutes=self.cfg.scheduling.past_grace_minutes):
            return False
        if start_utc > now + timedelta(days=self.cfg.scheduling.horizon_days):
            return False
        return True


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
