"""Operations: alerts, sync health, backups, the single-instance lock, a broken
config.yaml, the activity log and LSP's event filter."""
from __future__ import annotations

import json
import os

import pytest
import yaml

from app.alerts import Alerter
from app.config import LspConfig
from app.lsp_client import LspClient
from app.runtime import read_log_tail
from app.settings_store import SettingsStore
from app.state import State

from .conftest import raw_config
from .test_manager import manager


# -- alerts ----------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(Alerter, "_send", lambda self, url, text: bool(url) and not out.append(text))
    return out


def test_alert_after_two_failed_passes_then_on_recovery(sent):
    a = Alerter()
    a.pass_done("http://hook", False, "LSP login refused", [])
    assert sent == []  # one blip isn't worth waking anyone
    a.pass_done("http://hook", False, "LSP login refused", [])
    a.pass_done("http://hook", False, "LSP login refused", [])
    assert len(sent) == 1 and "LSP login refused" in sent[0]
    a.pass_done("http://hook", True, "", [])
    assert len(sent) == 2 and "works again" in sent[1]


def test_each_sheet_warning_is_sent_once(sent):
    a = Alerter()
    w = [{"key": "Fall!B: x", "text": "VB vs Pitt has no AM/PM"}]
    a.pass_done("http://hook", True, "", w)
    a.pass_done("http://hook", True, "", w)
    a.pass_done("http://hook", False, "sheet unreadable", None)  # broke off: no news
    a.pass_done("http://hook", True, "", w)
    assert sent == ["LiveScheduleSheets needs attention:\n• VB vs Pitt has no AM/PM"]
    # Fixed, then broken again: said again. And it all survives a restart.
    a.pass_done("http://hook", True, "", [])
    Alerter(a.to_dict()).pass_done("http://hook", True, "", w)
    assert len(sent) == 2


def test_no_webhook_sends_nothing(sent):
    a = Alerter()
    for _ in range(3):
        a.pass_done("", False, "down", [{"key": "k", "text": "t"}])
    assert sent == []


# -- sync health and status that survives a restart -------------------------------

def test_sync_health_and_saved_status(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    assert m.sync_health() == (True, "starting: no sync pass yet")
    failed = {"errors": 1, "problems": ["GetAllChannels failed (401)"], "warnings": []}
    m._finished(failed, "scheduled")
    assert m.sync_health()[0]  # one blip isn't "broken", as with alerts
    m._finished(failed, "scheduled")
    ok, why = m.sync_health()
    assert not ok and "401" in why and "last 2 syncs failed" in why
    m._finished({"errors": 0, "problems": [], "warnings": []}, "scheduled")
    assert m.sync_health() == (True, "ok")
    # A restart keeps the Status card and the alert record.
    again = manager(tmp_path, key_file, monkeypatch)
    assert again.last_run["trigger"] == "scheduled" and again.sync_health()[0]
    again.last_clean -= 4 * 3600  # nothing clean for hours
    assert "no sync pass without errors" in again.sync_health()[1]


def test_healthz_sync_endpoint():
    from app.webui import create_app

    class M:
        def sync_health(self):
            return False, "no sync pass without errors for 90 minutes"

    client = create_app(M(), store=None).test_client()
    r = client.get("/healthz/sync")
    assert r.status_code == 503 and b"90 minutes" in r.data


# -- backups, the lock, a broken config.yaml -------------------------------------

def test_state_is_backed_up_and_restored_from_the_backup(tmp_path):
    path = str(tmp_path / "state.json")
    s = State(path)
    s.events["r1"] = {"name": "VB vs Pitt", "bookings": {}}
    s.save()
    [backup] = os.listdir(tmp_path / "backups")
    assert backup.startswith("state.json.")
    with open(path, "w") as fh:
        fh.write('{"events": {')  # cut off
    restored = State(path)
    assert "r1" in restored.events and "restored state.json." in restored.load_error


def test_only_one_copy_may_use_the_data_folder(tmp_path):
    path = str(tmp_path / "state.json")
    first = State(path, exclusive=True)
    with pytest.raises(OSError, match="Another copy"):
        State(path, exclusive=True)
    first._lock_fh.close()
    State(path, exclusive=True)  # free again once the first is gone


def test_broken_config_yaml_falls_back_to_the_last_saved(tmp_path, key_file):
    path = tmp_path / "config.yaml"
    store = SettingsStore(str(path), str(tmp_path / "missing-seed.yaml"))
    store.save(raw_config(key_file, str(tmp_path / "state.json")))
    assert oct(os.stat(str(path) + ".lastgood").st_mode & 0o777) == oct(os.stat(path).st_mode & 0o777)
    path.write_text("sheet: [unclosed\n")  # a hand edit gone wrong
    assert store.load()["lsp"]["base_url"] == "http://lsp.invalid"
    assert "put back" in store.load_error
    assert any(p.name.startswith("config.yaml.broken-") for p in tmp_path.iterdir())
    assert yaml.safe_load(path.read_text())["lsp"]["base_url"] == "http://lsp.invalid"


# -- activity log ----------------------------------------------------------------

def test_activity_reads_the_end_of_the_log(tmp_path):
    (tmp_path / "liveschedulesheets.log").write_text(
        "a INFO app.sync: Created 'VB vs Pitt' on 01 - PCR B\n"
        "b INFO app.sheets: Tab 'Fall': parsed 3 schedulable event(s)\n"
        "c WARNING app.sync: Locked 'VB vs Duke': name changed in LSP\n")
    assert len(read_log_tail(str(tmp_path))) == 3
    assert [ln[0] for ln in read_log_tail(str(tmp_path), changes_only=True)] == ["a", "c"]
    assert read_log_tail(str(tmp_path / "none")) == []


def test_lsp_reads_are_retried():
    c = LspClient(LspConfig(base_url="http://lsp.invalid", verify_ssl=True, username="",
                            password=""))
    retry = c.session.get_adapter("http://lsp.invalid").max_retries
    assert retry.total == 2 and "GET" in retry.allowed_methods and "POST" not in retry.allowed_methods


def test_status_file_is_json(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    m._finished({"errors": 0, "problems": [], "warnings": []}, "manual")
    data = json.loads((tmp_path / "status.json").read_text())
    assert data["last_ok"] is True and data["last_run"]["trigger"] == "manual"


# -- second ops audit -------------------------------------------------------------

def test_webhook_payload_suits_the_service():
    from app.alerts import _payload
    assert _payload("https://hooks.slack.com/services/T/B/x", "hi") == {"text": "hi"}
    assert _payload("https://discord.com/api/webhooks/1/x", "hi") == {"content": "hi"}
    teams = _payload("https://prod-1.westus.logic.azure.com/workflows/abc/triggers/x", "a\nb")
    card = teams["attachments"][0]["content"]
    assert teams["type"] == "message" and card["type"] == "AdaptiveCard"
    assert [b["text"] for b in card["body"]] == ["a", "b"]
    assert _payload("https://alerts.example/hook", "hi") == {"text": "hi", "content": "hi"}


def test_webhook_url_is_kept_from_the_browser(tmp_path, key_file):
    path = tmp_path / "config.yaml"
    store = SettingsStore(str(path), str(tmp_path / "missing-seed.yaml"))
    store.save(raw_config(key_file, str(tmp_path / "state.json"),
                          runtime={"alert_webhook_url": "https://hooks.slack.com/services/T/B/secret"}))
    page = store.load_safe()
    assert "alert_webhook_url" not in page["runtime"]
    assert page["runtime"]["alert_webhook_set"] and page["runtime"]["alert_webhook_host"] == "hooks.slack.com"
    store.save_from_ui(page)  # saved back blank: kept
    assert store.load()["runtime"]["alert_webhook_url"].endswith("/secret")
    page = store.load_safe()
    page["runtime"]["alert_webhook_clear"] = True
    store.save_from_ui(page)
    assert "alert_webhook_url" not in store.load()["runtime"]


def test_a_valid_hand_edit_becomes_the_last_good_settings(tmp_path, key_file):
    import time
    path = tmp_path / "config.yaml"
    store = SettingsStore(str(path), str(tmp_path / "missing-seed.yaml"))
    store.save(raw_config(key_file, str(tmp_path / "state.json")))
    time.sleep(0.01)
    raw = store.load()
    raw["scheduling"]["lead_in_minutes"] = 25
    path.write_text(yaml.safe_dump(raw))  # edited by hand, validly
    store.load()
    path.write_text("scheduling: [oops\n")  # ...and later broken
    assert store.load()["scheduling"]["lead_in_minutes"] == 25


def test_clock_skew_ignores_a_slow_reply():
    c = LspClient(LspConfig(base_url="http://lsp.invalid", verify_ssl=True, username="",
                            password=""))
    c._skews.extend([2.0, 95.0, -1.5])  # the 95 s one was a slow reply
    assert c.clock_skew == -1.5
