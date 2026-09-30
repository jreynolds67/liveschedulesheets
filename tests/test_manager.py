"""SyncManager: status() while another operation holds the lock."""
from __future__ import annotations

import threading
import time

import pytest
import yaml

from app.manager import ManagerError, SyncManager
from app.settings_store import SettingsStore

from .conftest import raw_config


def manager(tmp_path, key_file, monkeypatch):
    # The test key file isn't a real service-account key.
    monkeypatch.setattr("app.manager.SheetReader", lambda cfg: object())
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw_config(key_file, str(tmp_path / "state.json"))))
    return SyncManager(SettingsStore(str(path), str(tmp_path / "missing-seed.yaml")))


def test_status_never_rebuilds_while_busy(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    first = m.status()
    assert first["config_ok"] and first["dry_run"] is False
    syncer = m._syncer

    def no_rebuild():
        raise AssertionError("status() rebuilt components during a sync")

    monkeypatch.setattr(m, "_components", no_rebuild)
    with m._lock:  # a sync pass in progress
        busy = m.status()
    assert busy["config_ok"] and busy["dry_run"] is False
    assert m._syncer is syncer


def test_status_reports_incomplete_config(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    raw = m.store.load()
    raw["sheet"]["labels"]["date"] = []
    m.store.save(raw)
    s = m.status()
    assert not s["config_ok"] and "labels.date" in s["config_error"]


def test_busy_status_still_reports_config_problems(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    raw = m.store.load()
    raw["sheet"]["labels"]["date"] = []
    m.store.save(raw)
    with m._lock:
        s = m.status()
    assert not s["config_ok"] and "labels.date" in s["config_error"]
    assert s["dry_run"] is False  # from the LSP half, which is still valid


def test_ui_reads_give_up_while_a_pass_runs(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    monkeypatch.setattr("app.manager._READ_WAIT_SECONDS", 0.05)
    with m._lock:
        with pytest.raises(ManagerError, match="sync pass is running"):
            m.preview()
        assert m.status()["busy"] is True
        assert m.test_connection()["ok"] is False


def test_problems_from_a_pass_become_last_error(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    m._finished({"problems": ["Could not open the Google Sheet: HttpError 403"]}, "scheduled")
    assert "403" in m.status()["last_error"]
    m._finished({"problems": []}, "scheduled")
    assert m.status()["last_error"] is None


def test_unreadable_state_file_is_a_config_error(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    (tmp_path / "state.json").mkdir()  # can't be opened as a file
    s = m.status()
    assert not s["config_ok"] and "state file" in s["config_error"]


def test_corrupt_state_file_is_reported(tmp_path, key_file, monkeypatch):
    (tmp_path / "state.json").write_text("{not json")
    m = manager(tmp_path, key_file, monkeypatch)
    assert "moved to" in m.status()["state_error"]


def test_stop_waits_for_the_pass(tmp_path, key_file, monkeypatch):
    m = manager(tmp_path, key_file, monkeypatch)
    finished = threading.Event()

    def slow_pass():
        m._stop.wait(5)  # a pass that notices shutdown between writes
        finished.set()
        return {"problems": []}

    m._components()
    monkeypatch.setattr(m._syncer, "run_once", slow_pass)
    m.start_loop()
    time.sleep(0.05)
    assert m.health() == (True, "ok")
    m.stop(timeout=5)
    assert finished.is_set() and not m._thread.is_alive()
    assert m.health()[0] is False
