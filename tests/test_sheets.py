"""Sheet parsing (pure; no Google calls)."""
from __future__ import annotations

import pytest

from app.config import parse_config
from app.sheets import (SheetError, SheetReader, _first_time_token, _normalize_room, a1_tab,
                        fill_merges, inspect_grid, locate_rows, parse_grid)

from .conftest import raw_config

GRID = [
    ["DATE", "9/12/2026", "9/13/2026", "9/14", "9/15/2026"],
    ["EVENT", "FB vs UCF", "WSOC vs Texas", "VB vs Pitt", ""],
    ["CREW CALL", "10:00 AM", "1:00 PM", "4:00 PM", ""],
    ["GAME START", "12:00 PM", "3:00 PM", "7:00 PM", "8:00 PM"],
    ["CONTROL ROOM", "PCR A", "b", "C", ""],
    ["SCOREBOARD", "", "", "", ""],
    ["CONTROL ROOM", "E", "E", "E", ""],
]


def cfg_for(tmp_path, **overrides):
    key = tmp_path / "key.json"
    key.write_text("{}")
    return parse_config(raw_config(str(key), str(tmp_path / "state.json"), **overrides))


def test_parse_grid_reads_each_column(tmp_path):
    events = parse_grid("Fall", GRID, cfg_for(tmp_path))
    # The column without a year is skipped; the empty-name column isn't an event.
    assert [(e.name, e.pcr, e.start.strftime("%m-%d %H:%M")) for e in events] == [
        ("FB vs UCF", "A", "09-12 11:50"),
        ("WSOC vs Texas", "B", "09-13 14:50"),
    ]
    assert events[0].end - events[0].start == __import__("datetime").timedelta(hours=3)


def test_first_control_room_row_wins(tmp_path):
    matches = locate_rows(GRID, 0, cfg_for(tmp_path).sheet.labels)
    assert matches["pcr"].row == 4 and matches["pcr"].method == "label"
    assert matches["time"].row == 3  # never CREW CALL


def test_keyword_and_content_detection(tmp_path):
    grid = [["Kickoff", "7:00 PM"], ["Matchup", "FB vs UCF"], ["", "9/12/2026"], ["Ctrl Room", "A"]]
    m = locate_rows(grid, 0, cfg_for(tmp_path).sheet.labels)
    assert (m["time"].row, m["time"].method) == (0, "keyword")
    assert (m["event_name"].row, m["event_name"].method) == (1, "keyword")
    assert m["pcr"].row == 3
    # One date cell isn't enough to guess a row from its contents.
    assert m["date"].row is None


def test_event_override_assigns_room_and_ignores(tmp_path):
    cfg = cfg_for(tmp_path, event_overrides={"Fall": [
        {"date": "2026-09-12", "event": "fb vs ucf", "control_room": "D"},
        {"date": "2026-09-13", "event": "WSOC vs Texas", "ignore": True},
    ]})
    events = parse_grid("Fall", GRID, cfg)
    assert [(e.name, e.pcr) for e in events] == [("FB vs UCF", "D")]


def test_inspect_grid_flags_missing_year(tmp_path):
    out = inspect_grid("Fall", GRID, cfg_for(tmp_path))
    by_col = {c["col"]: c for c in out["columns"]}
    assert by_col[4]["status"] == "no_start" and "no year" in by_col[4]["problem"]
    assert out["summary"]["ready"] == 2


@pytest.mark.parametrize("cell, want", [
    ("6:30 & 9:00 PM", "6:30 PM"),   # the AM/PM later in the cell applies
    ("7 PM", "7:00 PM"),
    ("7 p.m.", "7:00 PM"),
    ("Kickoff 3:30 PM (ESPN2)", "3:30 PM"),
    ("17:30", "17:30"),              # 24-hour
    ("9:00:00", "9:00"),             # 24-hour with seconds, as Sheets formats it
    ("12:00 NOON", "12:00 PM"),
    ("12 NOON", "12:00 PM"),
    ("Noon", "12:00 PM"),
    ("11:00 & 12 NOON", None),       # noon says nothing about the 11
    ("PRACTICE STARTS @2", None),    # bare number
    ("7:00", None),                  # 7 AM or 7 PM? refuse, don't guess
    ("7:00 ET", None),
])
def test_time_tokens(cell, want):
    assert _first_time_token(cell) == want


def test_inspect_grid_flags_missing_am_pm(tmp_path):
    grid = [["DATE", "9/12/2026"], ["EVENT", "FB vs UCF"], ["GAME START", "7:00"],
            ["CONTROL ROOM", "A"]]
    col = inspect_grid("Fall", grid, cfg_for(tmp_path))["columns"][0]
    assert col["status"] == "no_start" and "AM/PM" in col["problem"]
    assert parse_grid("Fall", grid, cfg_for(tmp_path)) == []


def test_doubleheader_numbering_ignores_missing_times(tmp_path):
    """Game 2 keeps occurrence 1 (and its fixes) while game 1's time is TBD."""
    grid = [["DATE", "9/12/2026", "9/12/2026"], ["EVENT", "BSB vs Duke", "BSB vs Duke"],
            ["GAME START", "TBD", "4:00 PM"], ["CONTROL ROOM", "A", "B"]]
    [game2] = parse_grid("Spring", grid, cfg_for(tmp_path))
    assert (game2.occurrence, game2.pcr) == (1, "B")
    grid[2][1] = "1:00 PM"
    assert [(e.occurrence, e.pcr) for e in parse_grid("Spring", grid, cfg_for(tmp_path))] == [
        (0, "A"), (1, "B")]


class _Google:
    """Stands in for the Sheets service: metadata and tab reads can fail."""

    def __init__(self, meta_error=None, bad_tabs=()):
        self.meta_error, self.bad_tabs = meta_error, bad_tabs


def _reader(tmp_path, google):
    r = SheetReader.__new__(SheetReader)  # skip the Google client setup
    r.cfg = cfg_for(tmp_path)

    def meta():
        if google.meta_error:
            raise google.meta_error
        return {"title": "t", "tabs": [{"name": n, "gid": 0, "hidden": False}
                                       for n in ("Fall", "Spring")]}

    def grid(tab):
        if tab in google.bad_tabs:
            raise RuntimeError("HttpError 500")
        return GRID

    r.sheet_meta, r.fetch_grid = meta, grid
    return r


def test_unreadable_sheet_is_an_error_not_an_empty_sheet(tmp_path):
    r = _reader(tmp_path, _Google(meta_error=RuntimeError("HttpError 403 caller does not have "
                                                           "permission")))
    with pytest.raises(SheetError, match="403"):
        r.read_events()


def test_one_bad_tab_is_reported(tmp_path):
    r = _reader(tmp_path, _Google(bad_tabs=("Spring",)))
    assert len(r.read_events()) == 2
    assert r.errors == ["Could not read tab 'Spring': HttpError 500"]


def test_room_normalisation():
    assert _normalize_room("PCR A") == "A"
    assert _normalize_room("Control Room B") == "B"
    assert _normalize_room("TBD") is None
    assert _normalize_room("PCRA") == "A"
    # The C in "PCR" is never taken for the room.
    for cell in ("PCR B", "pcr b", "PCRB", "PCR-B", "PCR: B", "PCR  B", "CR B"):
        assert _normalize_room(cell) == "B", cell
    assert _normalize_room("PCRC") == "C" and _normalize_room("PCR D (backup)") == "D"
    assert _normalize_room("PCR") is None
    assert _normalize_room("CR3") is None
    assert _normalize_room("HOME") is None
    assert _normalize_room("SHADOW") is None


def test_a1_tab_quotes_apostrophes():
    assert a1_tab("Football") == "'Football'"
    assert a1_tab("Women's Basketball") == "'Women''s Basketball'"


DOUBLEHEADER = [
    ["DATE", "9/12/2026", "9/12/2026"],
    ["EVENT", "BSB vs Duke", "BSB vs Duke"],
    ["GAME START", "1:00 PM", "6:00 PM"],
    ["CONTROL ROOM", "A", "A"],
]


def test_doubleheader_events_know_their_day(tmp_path):
    events = parse_grid("Spring", DOUBLEHEADER, cfg_for(tmp_path))
    assert [(e.occurrence, e.same_day, e.sheet_start) for e in events] == [
        (0, 2, "2026-09-12T13:00"), (1, 2, "2026-09-12T18:00")]


def test_event_fix_follows_its_game_when_a_doubleheader_game_is_removed(tmp_path):
    fixes = {"Spring": [
        # Game 1 ignored, game 2 moved to PCR C, made while both were in the sheet.
        {"date": "2026-09-12", "event": "BSB vs Duke", "occurrence": 0, "same_day": 2,
         "sheet_start": "2026-09-12T13:00", "ignore": True},
        {"date": "2026-09-12", "event": "BSB vs Duke", "occurrence": 1, "same_day": 2,
         "sheet_start": "2026-09-12T18:00", "control_room": "C"},
    ]}
    cfg = cfg_for(tmp_path, event_overrides=fixes)
    assert [(e.sheet_start, e.pcr) for e in parse_grid("Spring", DOUBLEHEADER, cfg)] == [
        ("2026-09-12T18:00", "C")]
    # Game 1's column deleted: game 2 is now game 0, but it isn't ignored.
    only_game_two = [row[:1] + row[2:] for row in DOUBLEHEADER]
    assert [(e.sheet_start, e.pcr) for e in parse_grid("Spring", only_game_two, cfg)] == [
        ("2026-09-12T18:00", "C")]


def test_event_fix_without_a_game_count_matches_by_number(tmp_path):
    cfg = cfg_for(tmp_path, event_overrides={"Spring": [
        {"date": "2026-09-12", "event": "BSB vs Duke", "occurrence": 1, "control_room": "D"}]})
    assert [e.pcr for e in parse_grid("Spring", DOUBLEHEADER, cfg)] == ["A", "D"]


def test_stray_spaces_and_invisible_characters_are_ignored(tmp_path):
    grid = [["DATE ", "9/12/2026\u00a0"], ["EVENT", " VB  vs\nPitt "],
            ["GAME\u00a0START", "7:00\u00a0PM"], ["\u200bCONTROL ROOM", "\u200bB"]]
    [ev] = parse_grid("Fall", grid, cfg_for(tmp_path))
    assert (ev.name, ev.pcr, ev.sheet_start) == ("VB vs Pitt", "B", "2026-09-12T19:00")


def test_event_fix_matches_whatever_the_spacing(tmp_path):
    cfg = cfg_for(tmp_path, event_overrides={"Fall": [
        {"date": "2026-09-12", "event": "VB vs  Pitt", "control_room": "D"}]})
    grid = [["DATE", "9/12/2026"], ["EVENT", "VB  vs Pitt"], ["GAME START", "7:00 PM"],
            ["CONTROL ROOM", "B"]]
    assert [e.pcr for e in parse_grid("Fall", grid, cfg)] == ["D"]


def test_merged_cells_apply_to_every_column_they_span():
    grid = [["DATE", "9/12/2026"], ["EVENT", "BSB vs Duke", "BSB vs Duke"],
            ["GAME START", "1:00 PM", "6:00 PM"], ["CONTROL ROOM", "A"], ["NOTES", "rain"],
            ["BANNER"]]
    merges = [{"startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 3},
              {"startRowIndex": 3, "endRowIndex": 5, "startColumnIndex": 1, "endColumnIndex": 3},
              {"startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 0, "endColumnIndex": 3}]
    out = fill_merges(grid, merges)
    assert out[0] == ["DATE", "9/12/2026", "9/12/2026"]
    assert out[3] == ["CONTROL ROOM", "A", "A"]
    assert out[4] == ["NOTES", "rain"]   # merged down: not copied into another row
    assert out[5] == ["BANNER"]          # starts in the label column: left alone


def test_postponed_or_cancelled_games_are_still_scheduled_but_flagged(tmp_path):
    grid = [["DATE", "9/12/2026", "9/13/2026", "9/14/2026"],
            ["EVENT", "VB vs Pitt", "VB vs Duke - CANCELLED", "Cancer Awareness Night"],
            ["GAME START", "7:00 PM - PPD", "7:00 PM", "7:00 PM"], ["CONTROL ROOM", "B", "B", "B"]]
    pitt, duke, awareness = parse_grid("Fall", grid, cfg_for(tmp_path))
    assert "“PPD”" in pitt.note and "“CANCELLED”" in duke.note
    assert awareness.note == ""


# -- what can't be scheduled is reported, not dropped silently ------------------

def _future(days=10):
    from datetime import date, timedelta
    d = date.today() + timedelta(days=days)
    return f"{d.month}/{d.day}/{d.year}"


def test_columns_that_cant_be_scheduled_are_reported(tmp_path):
    grid = [["DATE", _future(), _future(), _future(), "9/1/2020", _future(), ""],
            ["EVENT", "VB vs Pitt", "VB vs Duke", "VB vs UNC", "Old game", "VB vs Rice", "TBA game"],
            ["GAME START", "7:00", "TBD", "", "7:00", "7:00 PM", ""],
            ["CONTROL ROOM", "B", "B", "B", "B", "B", "B"]]
    problems = []
    events = parse_grid("Fall", grid, cfg_for(tmp_path), problems=problems)
    assert [e.name for e in events] == ["VB vs Rice"]
    # Only Pitt: TBD, blank, long past and blank-date columns aren't worth a warning.
    assert [p["text"] for p in problems] == [
        "“VB vs Pitt” (Fall column B) isn't scheduled: the time “7:00” has no AM/PM"]
    # Known by its cells, so inserting a column doesn't make it look new.
    assert problems[0]["key"] == f"Fall|VB vs Pitt|{_future()}|7:00"


def test_ignoring_a_column_silences_its_warning(tmp_path):
    from datetime import date, timedelta
    d = date.today() + timedelta(days=10)
    cfg = cfg_for(tmp_path, event_overrides={"Fall": [
        {"date": d.isoformat(), "event": "VB vs Pitt", "ignore": True}]})
    grid = [["DATE", _future()], ["EVENT", "VB vs Pitt"], ["GAME START", "7:00"], ["CONTROL ROOM", "B"]]
    problems = []
    parse_grid("Fall", grid, cfg, problems=problems)
    assert problems == []


def test_tab_whose_rows_cant_be_found_is_reported(tmp_path):
    problems = []
    assert parse_grid("Contacts", [["NAME", "Jane"], ["PHONE", "555"]], cfg_for(tmp_path),
                      problems=problems) == []
    assert "Tab “Contacts” is not read: no EVENT / DATE / start time row found" in problems[0]["text"]


def test_new_tabs_wait_to_be_turned_on(tmp_path):
    r = _reader(tmp_path, _Google())
    assert len(r.read_events(known_tabs=None)) == 4 and r.read_tabs == ["Fall", "Spring"]
    # "Spring" appeared since the tool last read the sheet: not read, and said so.
    assert len(r.read_events(known_tabs={"Fall"})) == 2 and r.read_tabs == ["Fall"]
    assert any("New tab “Spring”" in w["text"] for w in r.warnings)
    # Turned on in the UI: read.
    r.cfg = cfg_for(tmp_path, tab_overrides={"Spring": {"enabled": True}})
    assert len(r.read_events(known_tabs={"Fall"})) == 4


def test_settings_for_a_renamed_tab_are_reported(tmp_path):
    r = _reader(tmp_path, _Google())
    r.cfg = cfg_for(tmp_path, tab_overrides={"Football": {"enabled": False}})
    r.read_events()
    assert any("Settings for tab “Football” match no tab" in w["text"] for w in r.warnings)
