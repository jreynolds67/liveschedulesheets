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


# -- matching a sheet event to what the tool already made ------------------------

def test_renamed_tab_keeps_its_events(make_syncer, lsp):
    make_syncer([sheet_event(tab="Fall Olympic")]).run_once()
    s = make_syncer([sheet_event(tab="Fall Olympics")])
    assert statuses(s) == [EXISTS]
    s.run_once()
    [rec] = s.state.events.values()
    assert rec["tab"] == "Fall Olympics"
    assert all(b["by_tool"] for b in rec["bookings"].values())
    # ...so a later time change is still applied.
    s = make_syncer([sheet_event(tab="Fall Olympics", hour=21)])
    assert statuses(s) == [UPDATE]
    s.run_once()
    assert all("T20:50:00" in e["Start"] for e in lsp.events.values())


def test_another_sheet_events_booking_is_never_taken_for_a_hand_made_one(make_syncer, lsp):
    make_syncer([sheet_event(tab="Football")]).run_once()
    # The same game also listed on another tab.
    s = make_syncer([sheet_event(tab="Football"), sheet_event(tab="Composite")])
    items = s.plan()
    assert [i.status for i in items] == [EXISTS, EXISTS]
    assert "another sheet event" in items[1].message
    s.run_once()
    assert len(s.state.events) == 1 and len(lsp.events) == 2


def test_rename_after_kickoff_does_not_record_it_twice(key_file, state_path, lsp):
    from datetime import datetime
    from app.config import parse_config
    from app.models import ScheduledEvent
    from app.sync import Syncer
    from .conftest import TZ, raw_config

    def game(name):
        # Started 5 minutes ago: inside the 30-minute grace for new events.
        start = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(minutes=5)
        return ScheduledEvent(name=name, pcr="A", start=start, end=start + timedelta(hours=3),
                              source_tab="Football", source_column=2,
                              event_date=start.date().isoformat(),
                              sheet_start=start.strftime("%Y-%m-%dT%H:%M"))

    cfg = parse_config(raw_config(key_file, state_path))
    Syncer(cfg, FakeReader([game("FB vs UCF")]), lsp, State(state_path)).run_once()
    for e in lsp.events.values():  # it has started
        e["Start"] = (datetime.fromisoformat(e["Start"]) - timedelta(minutes=10)).isoformat()
    state = State(state_path)
    for rec in state.events.values():
        rec["start"] = (datetime.fromisoformat(rec["start"]) - timedelta(minutes=10)).isoformat()
    s = Syncer(cfg, FakeReader([game("FB vs UCF (Homecoming)")]), lsp, state)
    [item] = s.plan()
    assert item.status == EXISTS and item.started
    s.run_once()
    assert len(lsp.events) == 2


# -- hand-made bookings -------------------------------------------------------

def test_deleted_hand_made_event_is_booked_again(make_syncer, lsp):
    ev = sheet_event(pcr="B")
    hand = lsp.add_hand_made("b1", ev.name, ev.start, ev.end)
    make_syncer([ev]).run_once()
    del lsp.events[hand["Id"]]
    s = make_syncer([ev])
    assert statuses(s) == [UPDATE]
    assert s.run_once()["created"] == 1
    [e] = lsp.events.values()
    [rec] = s.state.events.values()
    assert rec["bookings"]["b1"] == {**rec["bookings"]["b1"], "event_id": e["Id"], "by_tool": True}


def test_hand_made_event_that_differs_from_the_sheet_says_so(make_syncer, lsp):
    ev = sheet_event(pcr="B", hour=19)
    lsp.add_hand_made("b1", ev.name, ev.start, ev.end)
    make_syncer([ev]).run_once()
    [item] = make_syncer([sheet_event(pcr="B", hour=21)]).plan()
    assert item.status == EXISTS
    assert "differs from the sheet" in item.message


# -- writes that fail or find LSP changed ---------------------------------------

def test_failed_update_keeps_the_times_lsp_has(make_syncer, lsp):
    make_syncer([sheet_event(hour=19)]).run_once()
    [rid] = make_syncer([]).state.events

    def fail(*_a, **_k):
        raise LspError("timed out")

    real, lsp.patch_event = lsp.patch_event, fail
    s = make_syncer([sheet_event(hour=21)])
    assert s.run_once()["errors"] == 2
    assert "T18:50:00" in s.state.events[rid]["start"]
    lsp.patch_event = real
    assert make_syncer([sheet_event(hour=21)]).run_once()["updated"] == 2


def test_partial_update_counts_as_started_from_the_earliest_channel(make_syncer, lsp):
    from app.state import record_start
    make_syncer([sheet_event(hour=19)]).run_once()
    real = lsp.patch_event

    def fail_a2(event_id, *a, **k):
        if lsp.events[event_id]["ChannelId"] == "a2":
            raise LspError("timed out")
        return real(event_id, *a, **k)

    lsp.patch_event = fail_a2
    s = make_syncer([sheet_event(hour=17)])  # moved earlier; only a1 takes it
    s.run_once()
    [rec] = s.state.events.values()
    assert "T18:50:00" in rec["start"]
    assert "T16:50:00" in record_start(rec).astimezone(sheet_event().start.tzinfo).isoformat()


def test_pcr_change_removes_an_event_lsp_gave_a_new_id(make_syncer, lsp):
    import uuid
    make_syncer([sheet_event(pcr="A")]).run_once()
    old = next(k for k, v in lsp.events.items() if v["ChannelId"] == "a1")
    e = lsp.events.pop(old)
    e["Id"] = str(uuid.uuid4())
    lsp.events[e["Id"]] = e
    s = make_syncer([sheet_event(pcr="B")])
    s.run_once()
    assert [v["ChannelId"] for v in lsp.events.values()] == ["b1"]


# -- LSP's own status, and changes made while a pass runs ----------------------

def test_event_started_early_in_lsp_is_never_changed(make_syncer, lsp):
    make_syncer([sheet_event(pcr="A")]).run_once()
    for e in lsp.events.values():
        e["Status"] = "Recording"  # an engineer started it ahead of time
    before = {k: dict(v) for k, v in lsp.events.items()}
    s = make_syncer([sheet_event(pcr="B", hour=21)])  # time and PCR changed in the sheet
    [item] = s.plan()
    assert item.status == EXISTS and item.started and "Recording in LSP" in item.message
    summary = s.run_once()
    assert (summary["updated"], summary["deleted"], summary["created"]) == (0, 0, 0)
    assert {k: dict(v) for k, v in lsp.events.items()} == before


def test_event_canceled_in_lsp_locks_and_cant_be_unlocked_while_canceled(make_syncer, lsp):
    make_syncer([sheet_event(pcr="B")]).run_once()
    [e] = lsp.events.values()
    e["Status"] = "Canceled"
    s = make_syncer([sheet_event(pcr="B", hour=21)])
    assert statuses(s) == [LOCKED]
    s.run_once()
    assert "T18:50:00" in e["Start"]
    [rid] = s.state.events
    with pytest.raises(LspError, match="canceled in LSP"):
        s.unlock(rid)
    # Deleted there instead: unlocking books it again.
    lsp.events.clear()
    s.unlock(rid)
    assert make_syncer([sheet_event(pcr="B", hour=21)]).run_once()["created"] == 1


def test_event_about_to_start_is_not_moved(key_file, state_path, lsp):
    from datetime import datetime
    from app.config import parse_config
    from app.models import ScheduledEvent
    from app.sync import Syncer
    from .conftest import TZ, raw_config

    def game(minutes):
        start = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(minutes=minutes)
        return ScheduledEvent(name="FB vs UCF", pcr="B", start=start,
                              end=start + timedelta(hours=3), source_tab="Football",
                              source_column=2, event_date=start.date().isoformat(),
                              sheet_start=start.strftime("%Y-%m-%dT%H:%M"))

    cfg = parse_config(raw_config(key_file, state_path))
    Syncer(cfg, FakeReader([game(1)]), lsp, State(state_path)).run_once()
    s = Syncer(cfg, FakeReader([game(60)]), lsp, State(state_path))
    [item] = s.plan()
    assert item.status == EXISTS and "About to start" in item.message
    assert s.run_once()["updated"] == 0


def test_event_edited_in_lsp_during_the_pass_is_not_overwritten(make_syncer, lsp):
    make_syncer([sheet_event(pcr="B")]).run_once()
    [e] = lsp.events.values()

    def engineer_edits(_event_id):
        e["End"] = "2099-01-01T00:00:00+00:00"  # extended by hand after the pass read LSP

    lsp.before_get_event = engineer_edits
    s = make_syncer([sheet_event(pcr="B", hour=21)])
    summary = s.run_once()
    assert summary["updated"] == 0 and "changed in LSP during this pass" in summary["problems"][0]
    assert "T18:50:00" in e["Start"] and e["End"].startswith("2099")
    lsp.before_get_event = None
    assert statuses(make_syncer([sheet_event(pcr="B", hour=21)])) == [LOCKED]


def test_engineer_making_the_sheets_change_in_lsp_first_is_not_a_conflict(make_syncer, lsp):
    make_syncer([sheet_event(pcr="B", hour=19)]).run_once()
    [e] = lsp.events.values()
    moved = sheet_event(pcr="B", hour=21)
    e.update(Start=moved.start.isoformat(), End=moved.end.isoformat())  # by hand, before the sheet
    s = make_syncer([moved])
    assert statuses(s) == [EXISTS]
    s.run_once()
    # ...and the event still follows the sheet afterwards.
    s = make_syncer([sheet_event(pcr="B", hour=20)])
    assert statuses(s) == [UPDATE]
    assert s.run_once()["updated"] == 1


def test_event_deleted_and_made_again_by_hand_is_not_taken_for_the_tools(make_syncer, lsp):
    make_syncer([sheet_event(pcr="B")]).run_once()
    [old] = lsp.events.values()
    lsp.events.clear()
    ev = sheet_event(pcr="B")
    mine = lsp.add_hand_made("b1", old["Name"], ev.start, ev.end)  # same values, by an engineer
    s = make_syncer([sheet_event(pcr="B", hour=21)])
    assert statuses(s) == [LOCKED]
    s.run_once()
    assert lsp.events[mine["Id"]]["Start"] == mine["Start"]
    assert s.delete_created()["deleted"] == 0 and mine["Id"] in lsp.events


def test_cleanup_skips_events_started_early_or_edited_by_hand(make_syncer, lsp):
    s = make_syncer([sheet_event(pcr="B"), sheet_event(name="VB vs Duke", pcr="B", days=3),
                     sheet_event(name="MSOC vs UNC", pcr="B", days=4)])
    s.run_once()
    by_name = {e["Name"]: e for e in lsp.events.values()}
    by_name["WSOC vs Texas"]["Status"] = "Recording"
    by_name["VB vs Duke"]["Name"] = "VB vs Duke (moved)"
    summary = make_syncer([]).delete_created()
    assert (summary["deleted"], summary["skipped_started"], summary["skipped_locked"]) == (1, 1, 1)
    assert sorted(e["Name"] for e in lsp.events.values()) == ["VB vs Duke (moved)", "WSOC vs Texas"]


# -- the event-name variable ---------------------------------------------------

@pytest.fixture
def var_syncer(key_file, state_path, lsp):
    from app.config import parse_config
    from app.sync import Syncer
    from .conftest import raw_config
    lsp.variable = "Event Name"
    cfg = parse_config(raw_config(key_file, state_path, lsp={"event_name_variable": "Event Name"}))
    return lambda events: Syncer(cfg, FakeReader(events), lsp, State(state_path))


def test_variable_set_by_hand_in_lsp_is_never_overwritten(var_syncer, lsp):
    var_syncer([sheet_event(pcr="B")]).run_once()
    [eid] = lsp.events
    assert lsp.variable_of(eid) == "WSOC vs Texas"
    lsp.set_variable(eid, "WSOC vs Texas SENIOR NIGHT")
    s = var_syncer([sheet_event(pcr="B", hour=21)])
    [item] = s.plan()
    assert item.status == LOCKED and "Event Name changed in LSP" in item.message
    s.run_once()
    assert lsp.variable_of(eid) == "WSOC vs Texas SENIOR NIGHT"
    assert "T18:50:00" in lsp.events[eid]["Start"]
    # Unlocking re-applies the sheet, the variable included.
    [rid] = s.state.events
    s.unlock(rid)
    assert var_syncer([sheet_event(pcr="B", hour=21)]).run_once()["updated"] == 1
    assert lsp.variable_of(eid) == "WSOC vs Texas" and "T20:50:00" in lsp.events[eid]["Start"]
    assert statuses(var_syncer([sheet_event(pcr="B", hour=21)])) == [EXISTS]


def test_variable_follows_a_sheet_rename_without_locking(var_syncer, lsp):
    var_syncer([sheet_event(pcr="B")]).run_once()
    [eid] = lsp.events
    s = var_syncer([sheet_event(name="WSOC vs Texas A&M", pcr="B")])  # step 3: same column
    assert s.run_once()["updated"] == 1
    assert lsp.variable_of(eid) == "WSOC vs Texas A&M"
    assert statuses(var_syncer([sheet_event(name="WSOC vs Texas A&M", pcr="B")])) == [EXISTS]


def test_variable_lsp_wont_keep_is_not_taken_for_a_hand_edit(var_syncer, lsp):
    lsp.keeps_variable = False
    var_syncer([sheet_event(pcr="B")]).run_once()
    for _ in range(2):
        assert statuses(var_syncer([sheet_event(pcr="B")])) == [EXISTS]
    assert statuses(var_syncer([sheet_event(pcr="B", hour=21)])) == [UPDATE]


# -- names and overlaps ----------------------------------------------------------

def test_hand_made_event_with_other_spacing_and_case_is_not_duplicated(make_syncer, lsp):
    ev = sheet_event(name="VB vs Pitt", pcr="B")
    lsp.add_hand_made("b1", "vb  vs   PITT ", ev.start, ev.end)
    s = make_syncer([ev])
    assert statuses(s) == [EXISTS]
    s.run_once()
    assert len(lsp.events) == 1


def test_name_lsp_shortens_is_not_resent_every_pass(make_syncer, lsp):
    lsp.name_limit = 10
    make_syncer([sheet_event(pcr="B")]).run_once()
    for _ in range(2):
        s = make_syncer([sheet_event(pcr="B")])
        assert statuses(s) == [EXISTS]
        assert s.run_once()["updated"] == 0


def test_overlapping_games_on_one_channel_are_flagged(make_syncer, lsp):
    s = make_syncer([sheet_event(name="BSB vs Duke", pcr="B", hour=13, col=2, same_day=2),
                     sheet_event(name="BSB vs Duke", pcr="B", hour=15, col=3, occurrence=1,
                                 same_day=2)])
    items = s.plan()
    assert all(i.status == CREATE for i in items)
    assert all("overlaps “BSB vs Duke”" in i.message for i in items)
    assert "overlaps" not in make_syncer([sheet_event(pcr="B")]).plan()[0].message
