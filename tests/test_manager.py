"""SyncManager: status() while another operation holds the lock."""
from __future__ import annotations

import yaml

from app.manager import SyncManager
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
