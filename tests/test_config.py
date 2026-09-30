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
