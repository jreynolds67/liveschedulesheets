"""Syncer end to end against the in-memory LSP."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.lsp_client import LspError
from app.state import State
from app.sync import CREATE, EXISTS, LOCKED, NOT_IN_SHEET, UPDATE

from .conftest import sheet_event


def statuses(syncer):
    return [i.status for i in syncer.plan()]


def test_creates_on_every_matching_channel_once(make_syncer, lsp):
    s = make_syncer([sheet_event(pcr="A")])
    summary = s.run_once()
    assert summary["created"] == 2
    assert sorted(e["ChannelId"] for e in lsp.events.values()) == ["a1", "a2"]
    # A second pass (new process, state from disk) changes nothing.
    s2 = make_syncer([sheet_event(pcr="A")])
    assert statuses(s2) == [EXISTS]
    assert s2.run_once()["created"] == 0
    assert len(lsp.events) == 2


def test_sheet_time_change_updates_in_place(make_syncer, lsp):
    make_syncer([sheet_event(hour=19)]).run_once()
    ids = set(lsp.events)
    s = make_syncer([sheet_event(hour=20)])
    assert statuses(s) == [UPDATE]
    assert s.run_once()["updated"] == 2
    assert set(lsp.events) == ids
    assert all("T19:50:00" in e["Start"] for e in lsp.events.values())


def test_hand_edit_locks_and_is_left_alone(make_syncer, lsp):
    make_syncer([sheet_event()]).run_once()
    edited = next(iter(lsp.events.values()))
    edited["Name"] = "Renamed by hand"
    s = make_syncer([sheet_event(hour=21)])
    assert statuses(s) == [LOCKED]
    s.run_once()
    assert edited["Name"] == "Renamed by hand"
    assert not any("T20:50:00" in e["Start"] for e in lsp.events.values())


def test_pcr_change_moves_channels(make_syncer, lsp):
    make_syncer([sheet_event(pcr="A")]).run_once()
    summary = make_syncer([sheet_event(pcr="B")]).run_once()
    assert (summary["created"], summary["deleted"]) == (1, 2)
    assert [e["ChannelId"] for e in lsp.events.values()] == ["b1"]


def test_gone_from_sheet_is_left_in_lsp(make_syncer, lsp):
    make_syncer([sheet_event()]).run_once()
    s = make_syncer([])
    assert statuses(s) == [NOT_IN_SHEET]
    s.run_once()
    assert len(lsp.events) == 2


def test_hand_made_event_is_not_duplicated(make_syncer, lsp):
    ev = sheet_event(pcr="B")
    lsp.add_hand_made("b1", ev.name, ev.start, ev.end)
    s = make_syncer([ev])
    assert statuses(s) == [EXISTS]
    s.run_once()
    assert len(lsp.events) == 1


# -- a pass that breaks off -----------------------------------------------------

def test_add_that_times_out_but_creates_is_tracked_as_the_tools(make_syncer, lsp, state_path):
    lsp.add_error = LspError("POST AddEvent failed: Read timed out")
    lsp.add_creates_anyway = True
    summary = make_syncer([sheet_event(pcr="B")]).run_once()
    assert (summary["created"], summary["errors"]) == (1, 0)
    booking = next(iter(State(state_path).events.values()))["bookings"]["b1"]
    assert booking["by_tool"] and booking["event_id"] in lsp.events
    # Next pass: the tool's own event, up to date, and cleanup would take it.
    lsp.add_error = None
    s = make_syncer([sheet_event(pcr="B")])
    assert statuses(s) == [EXISTS]
    assert len(s.state.tool_created()) == 1


def test_add_that_fails_outright_is_an_error(make_syncer, lsp):
    lsp.add_error = LspError("AddEvent failed (400)")
    summary = make_syncer([sheet_event(pcr="B")]).run_once()
    assert (summary["created"], summary["errors"]) == (0, 1)
    assert not lsp.events


def test_state_is_saved_when_a_pass_breaks_off(make_syncer, lsp, state_path):
    calls = {"n": 0}
    real_add = lsp.add_event

    def flaky_add(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("unexpected")
        return real_add(*args, **kwargs)

    lsp.add_event = flaky_add
    with pytest.raises(RuntimeError):
        make_syncer([sheet_event(pcr="A")]).run_once()
    saved = json.load(open(state_path))["events"]
    assert [list(r["bookings"]) for r in saved.values()] == [["a1"]]


def test_state_save_failure_is_raised(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    state = State(str(blocker / "state.json"))
    with pytest.raises(OSError):
        state.save()


def test_state_save_leaves_no_temp_file_on_failure(tmp_path, monkeypatch):
    state = State(str(tmp_path / "state.json"))
    state.events["x"] = {"bad": object()}  # not JSON-serialisable
    with pytest.raises(TypeError):
        state.save()
    assert list(tmp_path.iterdir()) == []


def test_untouched_after_a_new_event_arrives_later(make_syncer, lsp):
    """Adding a sheet event on a later pass doesn't disturb earlier ones."""
    first = sheet_event(name="VB vs Pitt", pcr="B", hour=18)
    make_syncer([first]).run_once()
    second = sheet_event(name="VB vs Duke", pcr="B", days=3)
    s = make_syncer([first, second])
    assert statuses(s) == [EXISTS, CREATE]
    assert s.run_once()["created"] == 1
    assert len(lsp.events) == 2


def test_lead_in_change_updates_unstarted_events(make_syncer, lsp):
    make_syncer([sheet_event()]).run_once()
    moved = sheet_event(lead_in=20)
    assert moved.start == sheet_event().start - timedelta(minutes=10)
    assert make_syncer([moved]).run_once()["updated"] == 2
