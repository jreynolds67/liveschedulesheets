"""SettingsStore: UI saves can't clobber other windows, the dry-run switch or
hand the LSP password to a new server."""
from __future__ import annotations

import pytest
import yaml

from app.config import effective_password
from app.settings_store import ConfigConflict, SettingsStore

from .conftest import raw_config


@pytest.fixture
def store(tmp_path, key_file):
    raw = raw_config(key_file, str(tmp_path / "state.json"),
                     lsp={"base_url": "http://lsp:6500", "username": "eng", "password": "pw",
                          "password_for": "http://lsp:6500"})
    raw["runtime"]["live"] = False
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return SettingsStore(str(path), str(tmp_path / "missing-seed.yaml"))


def ui_copy(store):
    """What the browser holds: load_safe(), as it would send it back."""
    return store.load_safe()


def test_general_save_never_changes_dry_run(store):
    page = ui_copy(store)
    store.set_live(True)  # another window goes live...
    page["runtime"]["live"] = False  # ...this stale one still shows dry run
    store.save_from_ui(page)
    assert store.load()["runtime"]["live"] is True
    store.set_live(False)
    page = ui_copy(store)
    page["runtime"]["live"] = True
    store.save_from_ui(page)
    assert store.load()["runtime"]["live"] is False


def test_stale_window_gets_a_conflict(store):
    a, b = ui_copy(store), ui_copy(store)
    a["scheduling"]["lead_in_minutes"] = 15
    store.save_from_ui(a)
    b["scheduling"]["horizon_days"] = 30
    with pytest.raises(ConfigConflict):
        store.save_from_ui(b)
    assert store.load()["scheduling"]["lead_in_minutes"] == 15
    store.save_from_ui({**b, "_version": None})  # scripts without a version still save


def test_dry_run_and_key_changes_are_not_conflicts(store):
    page = ui_copy(store)
    store.set_live(True)
    store.save_google_key(b'{"type": "service_account", "client_email": "a@b", '
                          b'"private_key": "k", "token_uri": "t"}')
    store.save_from_ui(page)  # no ConfigConflict


def test_changing_the_server_clears_the_password(store, monkeypatch):
    monkeypatch.setenv("LSP_PASSWORD", "from-env")
    page = ui_copy(store)
    page["lsp"]["base_url"] = "http://attacker.lan"
    out = store.save_from_ui(page)
    lsp = store.load()["lsp"]
    assert out["password_cleared"] and "password" not in lsp
    assert effective_password(lsp) == ""  # and LSP_PASSWORD doesn't follow it either
    page = ui_copy(store)
    page["lsp"]["password"] = "new"
    store.save_from_ui(page)
    assert effective_password(store.load()["lsp"]) == "new"


def test_password_kept_when_the_server_is_unchanged(store):
    page = ui_copy(store)
    page["lsp"]["username"] = "someone"
    assert not store.save_from_ui(page)["password_cleared"]
    assert effective_password(store.load()["lsp"]) == "pw"


def test_first_real_url_keeps_env_password(tmp_path, key_file, monkeypatch):
    monkeypatch.setenv("LSP_PASSWORD", "from-env")
    raw = raw_config(key_file, str(tmp_path / "s.json"),
                     lsp={"base_url": "https://live-schedule-pro.example.com"})
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(raw))
    store = SettingsStore(str(tmp_path / "c.yaml"), "")
    page = store.load_safe()
    page["lsp"]["base_url"] = "http://10.10.71.32:6500"
    assert not store.save_from_ui(page)["password_cleared"]
    assert effective_password(store.load()["lsp"]) == "from-env"


def test_load_safe_never_exposes_the_password(store):
    page = ui_copy(store)
    assert "password" not in page["lsp"] and "password_for" not in page["lsp"]
    assert page["lsp"]["password_set"] is True and page["_version"]


def test_hand_typed_yaml_dates_reach_the_ui_as_iso(store):
    text = open(store.path).read() + (
        "event_overrides:\n  Football:\n  - date: 2026-10-03\n    event: FB vs UCF\n"
        "    ignore: true\n")
    open(store.path, "w").write(text)
    [ov] = ui_copy(store)["event_overrides"]["Football"]
    assert ov["date"] == "2026-10-03"
    store.save_from_ui(ui_copy(store))
    assert yaml.safe_load(open(store.path))["event_overrides"]["Football"][0]["date"] == "2026-10-03"
