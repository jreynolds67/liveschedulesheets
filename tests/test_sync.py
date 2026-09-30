"""Syncer end to end against the in-memory LSP."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.lsp_client import LspError
from app.sheets import SheetError
from app.state import State
from app.sync import CREATE, EXISTS, LOCKED, NOT_IN_SHEET, OUT_OF_WINDOW, UPDATE

from .conftest import FakeReader, sheet_event


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


# -- longevity -----------------------------------------------------------------

def _age(syncer, days_ago):
    """Move every tracked record's event `days_ago` days into the past."""
    from datetime import datetime, timezone
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for rec in syncer.state.events.values():
        rec["start"] = when.isoformat()
        rec["end"] = (when + timedelta(hours=3)).isoformat()
    syncer.state.save()


def test_records_are_pruned_after_lsps_cleanup_threshold(make_syncer, lsp):
    s = make_syncer([sheet_event()])
    s.run_once()
    _age(s, 100)
    lsp.cleanup_days = 120
    s = make_syncer([])
    s.run_once()
    assert len(s.state.events) == 1
    lsp.cleanup_days = 90
    s = make_syncer([])
    s.run_once()
    assert s.state.events == {}
    assert make_syncer([]).state.events == {}  # and saved


def test_unreadable_threshold_falls_back_to_a_year(make_syncer, lsp):
    s = make_syncer([sheet_event()])
    s.run_once()
    lsp.cleanup_days = None  # settings call refused
    _age(s, 200)
    s = make_syncer([])
    s.run_once()
    assert len(s.state.events) == 1
    _age(s, 400)
    s = make_syncer([])
    s.run_once()
    assert s.state.events == {}


def test_cleanup_skips_events_that_have_started(make_syncer, lsp):
    s = make_syncer([sheet_event(pcr="B"), sheet_event(name="VB vs Duke", pcr="B", days=3)])
    s.run_once()
    started = next(r for r in s.state.events.values() if r["name"] == "WSOC vs Texas")
    started["start"] = "2020-01-01T12:00:00+00:00"
    summary = s.delete_created()
    assert (summary["deleted"], summary["skipped_started"]) == (1, 1)
    assert [e["Name"] for e in lsp.events.values()] == ["WSOC vs Texas"]


def test_missing_warning_only_covers_the_shown_window(make_syncer, lsp):
    s = make_syncer([sheet_event(pcr="B")])
    s.run_once()
    lsp.events.clear()  # deleted by hand in LSP
    assert [m["name"] for m in s.scheduled_events()["missing"]] == ["WSOC vs Texas"]
    _age(s, 10)
    assert s.scheduled_events(past_days=7)["missing"] == []
    assert len(s.scheduled_events(past_days=30)["missing"]) == 1


# -- safety ---------------------------------------------------------------------

def test_sheet_typo_outside_the_window_leaves_lsp_alone(make_syncer, lsp):
    good = sheet_event(days=3)
    make_syncer([good]).run_once()
    before = sorted(e["Start"] for e in lsp.events.values())
    typo = sheet_event(days=3 - 365)  # the year typed as last year
    s = make_syncer([typo])
    assert statuses(s) == [OUT_OF_WINDOW]
    s.run_once()
    assert sorted(e["Start"] for e in lsp.events.values()) == before
    # Fixing the sheet: back in step, nothing duplicated.
    s = make_syncer([good])
    assert statuses(s) == [EXISTS]
    s.run_once()
    assert len(lsp.events) == 2


def test_started_event_changed_by_lsp_is_not_locked(make_syncer, lsp):
    s = make_syncer([sheet_event()])
    s.run_once()
    for rec in s.state.events.values():
        rec["start"] = "2020-01-01T12:00:00+00:00"  # it has started
    s.state.save()
    for e in lsp.events.values():  # LSP rewrote End when it was stopped
        e["End"] = "2099-01-01T00:00:00+00:00"
    s = make_syncer([sheet_event()])
    assert statuses(s) == [EXISTS]
    s.run_once()
    assert not any(r["locked"] for r in s.state.events.values())


def test_channel_that_stops_matching_its_pcr_is_not_deleted(make_syncer, lsp):
    make_syncer([sheet_event(pcr="A")]).run_once()
    lsp.channels[1]["Name"] = "02 - SPARE CLEAN"  # renamed in LSP
    s = make_syncer([sheet_event(pcr="A")])
    [item] = s.plan()
    assert item.status == EXISTS
    assert s.run_once()["deleted"] == 0 and len(lsp.events) == 2


def test_interrupted_pcr_move_is_finished_next_pass(make_syncer, lsp):
    make_syncer([sheet_event(pcr="A")]).run_once()
    real_remove = lsp.remove_event

    def fail_remove(*a, **k):
        raise LspError("RemoveEvent failed (500)")

    lsp.remove_event = fail_remove
    summary = make_syncer([sheet_event(pcr="B")]).run_once()
    assert (summary["created"], summary["errors"]) == (1, 2)
    lsp.remove_event = real_remove
    summary = make_syncer([sheet_event(pcr="B")]).run_once()
    assert summary["deleted"] == 2
    assert [e["ChannelId"] for e in lsp.events.values()] == ["b1"]


def test_state_is_saved_after_each_write_and_shutdown_stops_the_pass(make_syncer, lsp, state_path):
    s = make_syncer([sheet_event(pcr="A"), sheet_event(name="VB vs Duke", pcr="B", days=3)])
    real_add = lsp.add_event

    def add_then_shutdown(*args, **kwargs):
        out = real_add(*args, **kwargs)
        on_disk = json.load(open(state_path))["events"] if calls else {}
        calls.append(sum(len(r["bookings"]) for r in on_disk.values()))
        s.stop.set()  # SIGTERM arrives during the first event's writes
        return out

    calls = []
    lsp.add_event = add_then_shutdown
    summary = s.run_once()
    assert calls == [0, 1]  # the first booking was on disk before the second write
    assert summary["created"] == 2 and "shutdown" in summary["problems"][0]
    assert [e["Name"] for e in lsp.events.values()] == ["WSOC vs Texas"] * 2  # Duke not started


def test_unreadable_sheet_fails_the_pass(make_syncer, lsp):
    class Broken(FakeReader):
        def read_events(self):
            raise SheetError("Could not open the Google Sheet: HttpError 403")

    s = make_syncer([])
    s.reader = Broken()
    summary = s.run_once()
    assert summary["errors"] == 1 and "403" in summary["problems"][0]


def test_a_tab_that_fails_is_counted(make_syncer, lsp):
    s = make_syncer([sheet_event()])
    s.reader.errors = ["Could not read tab 'Football': HttpError 500"]
    summary = s.run_once()
    assert summary["created"] == 2 and summary["errors"] == 1
    assert summary["problems"] == ["Could not read tab 'Football': HttpError 500"]


def test_corrupt_state_file_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"events": {"x": ')  # cut off mid-write
    state = State(str(path))
    assert state.events == {} and "moved to" in state.load_error
    [aside] = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
    assert aside.read_text() == '{"events": {"x": '
    assert not path.exists()


# -- started events, doubleheaders, double bookings -----------------------------

def test_started_event_is_never_moved_by_a_later_sheet_change(make_syncer, lsp):
    s = make_syncer([sheet_event(hour=19)])
    s.run_once()
    for rec in s.state.events.values():
        rec["start"] = "2020-01-01T12:00:00+00:00"  # it has started
    s.state.save()
    before = {k: dict(v) for k, v in lsp.events.items()}
    # A weather delay: the sheet moves the game later the same day. The
    # started record must keep its times, so later passes leave it alone too.
    for _ in range(3):
        s = make_syncer([sheet_event(hour=21)])
        assert statuses(s) == [EXISTS]
        s.run_once()
    assert {k: dict(v) for k, v in lsp.events.items()} == before
    assert all(r["start"] == "2020-01-01T12:00:00+00:00" for r in s.state.events.values())


def doubleheader(*games):
    """Sheet events for same-named games on one day: (hour, column) pairs."""
    return [sheet_event(name="BSB vs Duke", hour=h, col=c, occurrence=i, same_day=len(games))
            for i, (h, c) in enumerate(games)]


def test_removing_game_one_of_a_doubleheader_leaves_game_two_alone(make_syncer, lsp):
    make_syncer(doubleheader((13, 2), (18, 3))).run_once()
    before = {k: dict(v) for k, v in lsp.events.items()}
    # Game 1's column is deleted: game 2 is now the day's only (first) game.
    s = make_syncer(doubleheader((18, 2)))
    items = s.plan()
    assert sorted(i.status for i in items) == [EXISTS, NOT_IN_SHEET]
    s.run_once()
    assert {k: dict(v) for k, v in lsp.events.items()} == before  # nothing moved


def test_adding_a_game_before_a_tracked_one_keeps_it(make_syncer, lsp):
    make_syncer(doubleheader((18, 2))).run_once()
    ids = set(lsp.events)
    # An earlier game is added in front: the tracked game becomes game 2.
    s = make_syncer(doubleheader((13, 2), (18, 3)))
    assert sorted(i.status for i in s.plan()) == [CREATE, EXISTS]
    s.run_once()
    assert ids < set(lsp.events) and len(lsp.events) == 4


def test_update_never_lands_on_another_event_with_the_same_name_and_start(make_syncer, lsp):
    make_syncer([sheet_event(hour=19)]).run_once()
    ev = sheet_event(hour=21)
    lsp.add_hand_made("a1", ev.name, ev.start, ev.end)
    s = make_syncer([ev])
    [item] = s.plan()
    by_channel = {t.channel_id: t.status for t in item.targets}
    assert by_channel == {"a1": EXISTS, "a2": UPDATE}
    assert "already has this name and start" in item.message
    s.run_once()
    on_a1 = sorted(e["Start"] for e in lsp.events.values() if e["ChannelId"] == "a1")
    assert len(on_a1) == 2 and len(set(on_a1)) == 2  # the tool's one wasn't moved onto it


def test_pcr_change_adopts_a_hand_made_event_on_the_new_channel(make_syncer, lsp):
    make_syncer([sheet_event(pcr="A")]).run_once()
    ev = sheet_event(pcr="B")
    lsp.add_hand_made("b1", ev.name, ev.start, ev.end)
    s = make_syncer([ev])
    summary = s.run_once()
    assert (summary["created"], summary["deleted"]) == (0, 2)
    assert [e["ChannelId"] for e in lsp.events.values()] == ["b1"]
    [rec] = s.state.events.values()
    assert rec["bookings"]["b1"]["by_tool"] is False


def test_dry_run_does_not_record_hand_made_events(key_file, state_path, lsp):
    from app.config import parse_config
    from app.sync import Syncer
    from .conftest import raw_config
    cfg = parse_config(raw_config(key_file, state_path, runtime={"live": False}))
    ev = sheet_event(pcr="B")
    lsp.add_hand_made("b1", ev.name, ev.start, ev.end)
    s = Syncer(cfg, FakeReader([ev]), lsp, State(state_path))
    assert statuses(s) == [EXISTS]
    s.run_once()
    assert s.state.events == {}
