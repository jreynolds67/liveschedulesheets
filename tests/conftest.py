"""Shared fixtures: a config, an in-memory LSP and a stub sheet reader, so the
sync logic runs end to end without Google or a real LSP server."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import parse_config
from app.lsp_client import LspError
from app.models import ScheduledEvent
from app.state import State
from app.sync import Syncer

TZ = ZoneInfo("America/New_York")


def raw_config(key_file: str, state_file: str, **overrides) -> dict:
    raw = {
        "sheet": {
            "spreadsheet_id": "1TNVdUIEy1TNRAzBVEWmRnzxO-vwV3AzbYwDzm6GGOSQ",
            "tabs": [],
            "header_column": "A",
            "labels": {"event_name": ["EVENT"], "pcr": ["CONTROL ROOM", "PCR"],
                       "date": ["DATE"], "time": ["GAME START", "START TIME"]},
            "timezone": "America/New_York",
        },
        "date_parsing": {"skip_values": ["TBD", "TBA"]},
        "scheduling": {"lead_in_minutes": 10, "safety_cap_hours": 3, "horizon_days": 60,
                       "past_grace_minutes": 30, "event_name_prefix": ""},
        "pcr_channel_map": {"A": {"channel_match": "PCR A"}, "B": {"channel_match": "PCR B"}},
        "lsp": {"base_url": "http://lsp.invalid", "event_name_variable": ""},
        "runtime": {"live": True, "state_file": state_file},
        "google": {"credentials_file": key_file},
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return raw


@pytest.fixture
def key_file(tmp_path):
    p = tmp_path / "key.json"
    p.write_text("{}")
    return str(p)


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "state.json")


@pytest.fixture
def cfg(key_file, state_path):
    return parse_config(raw_config(key_file, state_path))


class FakeLsp:
    """Just enough of LspClient, holding events in memory."""

    def __init__(self, channels=None):
        self.channels = channels if channels is not None else [
            {"Id": "a1", "Name": "01 - PCR A PGM"}, {"Id": "a2", "Name": "02 - PCR A CLEAN"},
            {"Id": "b1", "Name": "01 - PCR B PGM"},
        ]
        self.events: dict[str, dict] = {}
        self.auth_mode = "none"
        self.cleanup_days = 365
        self.add_error = None          # exception AddEvent raises
        self.add_creates_anyway = False  # ...after storing the event (a timeout)
        self.removed: list[str] = []

    def get_all_channels(self):
        return copy.deepcopy(self.channels)

    def get_events_for_channel(self, channel_id):
        return [copy.deepcopy(e) for e in self.events.values() if e["ChannelId"] == channel_id]

    def get_event(self, event_id):
        e = self.events.get(event_id)
        return copy.deepcopy(e) if e else None

    def get_general_settings(self):
        if self.cleanup_days is None:
            raise LspError("GET /api/v1/settings/general failed (403)")
        return {"EventCleanupThresholdInDays": self.cleanup_days}

    def add_event(self, ev, channel_id, name, force=False):
        e = {"Id": str(uuid.uuid4()), "Name": name, "Start": ev.start.isoformat(),
             "End": ev.end.isoformat(), "ChannelId": channel_id}
        if self.add_error is not None:
            if self.add_creates_anyway:
                self.events[e["Id"]] = e
            raise self.add_error
        self.events[e["Id"]] = e
        return copy.deepcopy(e)

    def patch_event(self, event_id, name, start, end, force=False, extra=None):
        self.events[event_id].update(Name=name, Start=start.isoformat(), End=end.isoformat(),
                                     **(extra or {}))

    def remove_event(self, event_id, force=False):
        self.removed.append(event_id)
        self.events.pop(event_id, None)

    def add_hand_made(self, channel_id, name, start, end):
        e = {"Id": str(uuid.uuid4()), "Name": name, "Start": start.isoformat(),
             "End": end.isoformat(), "ChannelId": channel_id}
        self.events[e["Id"]] = e
        return e


class FakeReader:
    def __init__(self, events=None):
        self.events = events or []

    def read_events(self):
        return copy.deepcopy(self.events)


def sheet_event(name="WSOC vs Texas", pcr="A", days=2, hour=19, tab="Fall Olympic", col=2,
                occurrence=0, lead_in=10, cap_hours=3.0) -> ScheduledEvent:
    """A sheet event `days` from today, as parse_grid would produce it."""
    day = (datetime.now(TZ) + timedelta(days=days)).date()
    kickoff = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
    start = kickoff - timedelta(minutes=lead_in)
    return ScheduledEvent(name=name, pcr=pcr, start=start, end=start + timedelta(hours=cap_hours),
                          source_tab=tab, source_column=col, event_date=day.isoformat(),
                          occurrence=occurrence)


@pytest.fixture
def lsp():
    return FakeLsp()


@pytest.fixture
def make_syncer(cfg, lsp, state_path):
    def make(events, config=None):
        return Syncer(config or cfg, FakeReader(events), lsp, State(state_path))
    return make
