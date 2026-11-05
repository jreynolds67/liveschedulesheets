"""Config validation: bad numbers are errors, not crashes or runaway loops."""
from __future__ import annotations

import pytest

from app.config import ConfigError, load_raw, parse_config

from .conftest import raw_config


@pytest.mark.parametrize("section, values, message", [
    ("runtime", {"poll_interval_seconds": 0}, "between 60"),         # a blank UI field
    ("runtime", {"poll_interval_seconds": "abc"}, "whole number"),
    ("scheduling", {"lead_in_minutes": -5}, "lead_in_minutes"),
    ("scheduling", {"safety_cap_hours": 0}, "safety_cap_hours"),
    ("scheduling", {"horizon_days": 2.5}, "whole number"),
    ("event_overrides", {"Fall": [{"date": "2026-09-12", "event": "x", "occurrence": "two"}]},
     "bad occurrence"),
])
def test_bad_numbers_are_config_errors(key_file, state_path, section, values, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw_config(key_file, state_path, **{section: values}))


def test_missing_numbers_use_defaults(key_file, state_path):
    raw = raw_config(key_file, state_path)
    del raw["scheduling"]["horizon_days"]
    raw["runtime"]["poll_interval_seconds"] = None
    cfg = parse_config(raw)
    assert (cfg.scheduling.horizon_days, cfg.runtime.poll_interval_seconds) == (60, 900)


def test_bad_yaml_is_a_config_error(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("sheet: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_raw(str(p))


def test_password_is_only_sent_to_the_url_it_was_entered_for(key_file, state_path, monkeypatch):
    monkeypatch.setenv("LSP_PASSWORD", "from-env")
    raw = raw_config(key_file, state_path, lsp={"base_url": "http://lsp:6500/",
                                                "password": "pw", "password_for": "http://lsp:6500"})
    assert parse_config(raw).lsp.password == "pw"
    raw["lsp"]["base_url"] = "http://elsewhere"
    assert parse_config(raw).lsp.password == ""  # neither the stored nor the env one
    del raw["lsp"]["password_for"], raw["lsp"]["password"]
    assert parse_config(raw).lsp.password == "from-env"  # never bound: first setup


def test_extra_rooms_default_to_basketball_in_c_turned_off(key_file, state_path):
    sc = parse_config(raw_config(key_file, state_path)).scheduling
    assert [(x.sport, x.pcr, x.enabled) for x in sc.extra_rooms] == [("MBB", "C", False),
                                                                     ("WBB", "C", False)]
    assert sc.rooms_for("MBB vs Duke", "A") == ["A"]


def test_extra_rooms_for_a_sport(key_file, state_path):
    sc = parse_config(raw_config(key_file, state_path, scheduling={"extra_rooms": [
        {"sport": "MBB", "pcr": "pcr c"}, {"sport": "WBB", "pcr": "C", "enabled": False}]})).scheduling
    assert sc.rooms_for("MBB vs Duke", "A") == ["A", "C"]
    assert sc.rooms_for("mbb vs Duke", "C") == ["C"]
    assert sc.rooms_for("WBB vs UNC", "A") == ["A"]
    assert sc.rooms_for("XMBB vs Duke", "A") == ["A"]
    assert sc.rooms_for("MBB vs Duke", "") == []


def test_extra_room_row_needs_a_pcr(key_file, state_path):
    with pytest.raises(ConfigError):
        parse_config(raw_config(key_file, state_path, scheduling={"extra_rooms": [{"sport": "MBB"}]}))


def test_extra_room_start_offset(key_file, state_path):
    def offsets(minutes):
        return parse_config(raw_config(key_file, state_path, scheduling={"extra_rooms": [
            {"sport": "MBB", "pcr": "C", "game_offset": minutes}]})).scheduling.room_offsets(
            "MBB vs Duke", "A")
    assert offsets("-50") == [("A", None), ("C", -50)]
    assert offsets("+50") == offsets(50) == [("A", None), ("C", 50)]
    assert offsets("0") == offsets(0) == [("A", None), ("C", 0)]
    assert offsets("") == offsets(None) == [("A", None), ("C", None)]
    for bad in ("soon", 24 * 60 + 1):
        with pytest.raises(ConfigError):
            offsets(bad)


def test_offset_saved_under_its_first_name_is_read(key_file, state_path):
    sc = parse_config(raw_config(key_file, state_path, scheduling={"extra_rooms": [
        {"sport": "MBB", "pcr": "C", "minutes": -50}]})).scheduling
    assert sc.room_offsets("MBB vs Duke", "A") == [("A", None), ("C", -50)]
