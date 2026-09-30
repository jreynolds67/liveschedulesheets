"""Web API guards: cross-site writes and Basic auth."""
from __future__ import annotations

import pytest

from app.webui import create_app


class StubManager:
    def status(self):
        return {"ok": True}

    def delete_created(self):
        return {"deleted": 0}


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
