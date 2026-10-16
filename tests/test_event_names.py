"""How sheet event names become LSP names (the event and its Event Name
variable): home games get the home team, two-team names are kept."""
from __future__ import annotations

import pytest

from app.config import parse_config
from app.models import format_event_name
from app.state import State
from app.sync import CREATE, EXISTS, UPDATE, Syncer

from .conftest import FakeReader, raw_config, sheet_event


@pytest.mark.parametrize("sheet, lsp", [
    ("MSOC vs UNC", "MSOC UNC vs. Pitt"),
    ("MSOC vs. UNC", "MSOC UNC vs. Pitt"),
    ("WSOC VS Duke", "WSOC Duke vs. Pitt"),
    ("MBB vs BOSTON COLLEGE", "MBB BOSTON COLLEGE vs. Pitt"),
    ("VB Jacksonville vs High Point", "VB Jacksonville vs. High Point"),  # two teams given
    ("UNC vs. Duke", "UNC vs. Duke"),                                      # no sport code
    ("MSOC vs Georgetown (8 PM)", "MSOC Georgetown vs. Pitt"),            # notes dropped
    ("MBB vs Towson (EXH)", "MBB Towson vs. Pitt"),
    ("FB vs Miami (OH)", "FB Miami (OH) vs. Pitt"),                       # part of the name
    ("10/25 MSOC vs Virginia (7 PM)", "10/25 MSOC Virginia vs. Pitt"),
    ("Senior Night (7 PM)", "Senior Night (7 PM)"),                       # no "vs": as is
])
def test_format(sheet, lsp):
    assert format_event_name(sheet, "Pitt") == lsp


def test_no_home_team_leaves_names_alone():
    assert format_event_name("MSOC vs UNC (7 PM)", "") == "MSOC vs UNC (7 PM)"


def test_settings(key_file, state_path):
    raw = raw_config(key_file, state_path)
    del raw["scheduling"]["home_team"]
    sc = parse_config(raw).scheduling
    assert sc.home_team == "Pitt"  # on by default
    raw["scheduling"].update(home_team="Panthers", sport_codes=["ESPORTS"],
                             sport_safety_caps=[{"sport": "RIFLE", "hours": 2}],
                             event_name_prefix="LIVE ")
    sc = parse_config(raw).scheduling
    for sheet, lsp in [("ESPORTS vs UNC", "LIVE ESPORTS UNC vs. Panthers"),
                       ("RIFLE vs Navy", "LIVE RIFLE Navy vs. Panthers"),  # from record lengths
                       ("MSOC vs UNC", "LIVE MSOC vs. UNC")]:            # not in the custom list
        assert sc.lsp_name(sheet_event(name=sheet)) == lsp


@pytest.fixture
def home_syncer(key_file, state_path, lsp):
    lsp.variable = "Event Name"
    cfg = parse_config(raw_config(key_file, state_path, scheduling={"home_team": "Pitt"},
                                  lsp={"event_name_variable": "Event Name"}))
    return lambda events: Syncer(cfg, FakeReader(events), lsp, State(state_path))


def test_lsp_gets_the_formatted_name_on_the_event_and_variable(home_syncer, lsp):
    s = home_syncer([sheet_event(name="MSOC vs UNC (7 PM)", pcr="B")])
    [item] = s.plan()
    assert (item.status, item.lsp_name) == (CREATE, "MSOC UNC vs. Pitt")
    s.run_once()
    [e] = lsp.events.values()
    assert e["Name"] == "MSOC UNC vs. Pitt" and lsp.variable_of(e["Id"]) == "MSOC UNC vs. Pitt"
    assert [i.status for i in home_syncer([sheet_event(name="MSOC vs UNC (7 PM)", pcr="B")]).plan()] == [EXISTS]


def test_events_already_in_lsp_are_renamed(key_file, state_path, lsp):
    lsp.variable = "Event Name"
    old = parse_config(raw_config(key_file, state_path, lsp={"event_name_variable": "Event Name"}))
    Syncer(old, FakeReader([sheet_event(name="MSOC vs UNC", pcr="B")]), lsp,
           State(state_path)).run_once()
    new = parse_config(raw_config(key_file, state_path, scheduling={"home_team": "Pitt"},
                                  lsp={"event_name_variable": "Event Name"}))
    s = Syncer(new, FakeReader([sheet_event(name="MSOC vs UNC", pcr="B")]), lsp, State(state_path))
    assert [i.status for i in s.plan()] == [UPDATE]
    s.run_once()
    [e] = lsp.events.values()
    assert e["Name"] == "MSOC UNC vs. Pitt" and lsp.variable_of(e["Id"]) == "MSOC UNC vs. Pitt"


def test_hand_made_event_in_the_sheets_wording_is_not_duplicated(home_syncer, lsp):
    ev = sheet_event(name="MSOC vs UNC", pcr="B")
    lsp.add_hand_made("b1", "MSOC vs UNC", ev.start, ev.end)
    s = home_syncer([ev])
    assert [i.status for i in s.plan()] == [EXISTS]
    s.run_once()
    assert len(lsp.events) == 1
