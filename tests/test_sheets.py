"""Sheet parsing (pure; no Google calls)."""
from __future__ import annotations

import pytest

from app.config import parse_config
from app.sheets import (SheetError, SheetReader, _first_time_token, _normalize_room, a1_tab,
                        inspect_grid, locate_rows, parse_grid)

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
    assert _normalize_room("CR3") is None
    assert _normalize_room("HOME") is None
    assert _normalize_room("SHADOW") is None


def test_a1_tab_quotes_apostrophes():
    assert a1_tab("Football") == "'Football'"
    assert a1_tab("Women's Basketball") == "'Women''s Basketball'"
