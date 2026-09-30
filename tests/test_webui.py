"""Web API guards: cross-site writes and Basic auth."""
from __future__ import annotations

import pytest
import yaml

from app.settings_store import SettingsStore
from app.webui import create_app

from .conftest import raw_config


class StubManager:
    healthy = (True, "ok")

    def status(self):
        return {"ok": True}

    def delete_created(self):
        return {"deleted": 0}

    def health(self):
        return self.healthy


@pytest.fixture
def client():
    return create_app(StubManager(), store=None).test_client()


def test_same_origin_and_non_browser_posts_are_allowed(client):
    assert client.post("/api/delete-created").status_code == 200  # curl / Companion
    ok = client.post("/api/delete-created", headers={"Origin": "http://localhost",
                                                      "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_site_posts_are_refused(client, headers):
    assert client.post("/api/delete-created", headers=headers).status_code == 403


def test_cross_site_reads_still_work(client):
    assert client.get("/api/status", headers={"Origin": "http://evil.example"}).status_code == 200


def test_basic_auth(client, monkeypatch):
    monkeypatch.setenv("UI_USER", "eng")
    monkeypatch.setenv("UI_PASSWORD", "s3cret")
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", auth=("eng", "wrong")).status_code == 401
    assert client.get("/api/status", auth=("eng", "s3cret")).status_code == 200


@pytest.fixture
def store(tmp_path, key_file):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw_config(key_file, str(tmp_path / "state.json"))))
    return SettingsStore(str(path), "")


def test_config_save_from_a_stale_window_is_refused(store):
    client = create_app(StubManager(), store).test_client()
    page = client.get("/api/config").get_json()
    page["scheduling"]["lead_in_minutes"] = 20
    ok = client.post("/api/config", json=page).get_json()
    assert ok["saved"] and ok["valid"] and ok["version"] != page["_version"]
    stale = client.post("/api/config", json=page)  # same old _version
    assert stale.status_code == 409 and stale.get_json()["conflict"]


def test_dry_run_switch(store):
    client = create_app(StubManager(), store).test_client()
    assert client.post("/api/dry-run", json={"dry_run": False}).get_json()["ok"]
    assert store.load()["runtime"]["live"] is True
    assert client.post("/api/dry-run", json={"dry_run": True}).get_json()["ok"]
    assert store.load()["runtime"]["live"] is False
    assert not client.post("/api/dry-run", json={"dry_run": "no"}).get_json()["ok"]


def test_healthz_needs_no_auth(monkeypatch):
    monkeypatch.setenv("UI_USER", "eng")
    monkeypatch.setenv("UI_PASSWORD", "s3cret")
    manager = StubManager()
    client = create_app(manager, store=None).test_client()
    assert client.get("/healthz").status_code == 200
    manager.healthy = (False, "sync loop is not running")
    reply = client.get("/healthz")
    assert reply.status_code == 503 and b"not running" in reply.data


def test_status_says_whether_the_ui_has_a_password(client, monkeypatch):
    assert client.get("/api/status").get_json()["ui_auth"] is False
    monkeypatch.setenv("UI_PASSWORD", "x")
    assert client.get("/api/status", auth=("", "x")).get_json()["ui_auth"] is True
