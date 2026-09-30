"""LspClient error handling (no real server)."""
from __future__ import annotations

import pytest
import requests

from app.config import LspConfig
from app.lsp_client import LspClient, LspError


def client():
    return LspClient(LspConfig(base_url="http://lsp.invalid", verify_ssl=True,
                               username="", password=""), timeout=1)


class Reply:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def test_network_errors_become_lsp_errors(monkeypatch):
    c = client()

    def boom(*args, **kwargs):
        raise requests.ConnectTimeout("timed out")

    monkeypatch.setattr(c.session, "request", boom)
    with pytest.raises(LspError, match="timed out"):
        c.get_all_channels()


def test_non_json_reply_is_an_lsp_error(monkeypatch):
    c = client()
    monkeypatch.setattr(c.session, "request", lambda *a, **k: Reply(200, None, "<html>"))
    with pytest.raises(LspError, match="non-JSON"):
        c.get_events_for_channel("x")


def test_read_only_refuses_writes():
    c = client()
    c.read_only = True
    with pytest.raises(LspError, match="Dry run"):
        c.remove_event("x")


def test_general_settings(monkeypatch):
    c = client()
    monkeypatch.setattr(c.session, "request",
                        lambda *a, **k: Reply(200, {"EventCleanupThresholdInDays": 90}))
    assert c.get_general_settings()["EventCleanupThresholdInDays"] == 90


def _token_server(c, logins):
    """A server that wants a bearer token; `logins` are its /auth/login replies."""
    replies = iter(logins)
    c.session.post = lambda *a, **k: next(replies)

    def request(method, url, headers=None, auth=None, **kwargs):
        ok = (headers or {}).get("Authorization") == "Bearer t"
        return Reply(200, []) if ok else Reply(401, None, "unauthorized")

    c.session.request = request


def logged_in_client():
    return LspClient(LspConfig(base_url="http://lsp.invalid", verify_ssl=True,
                               username="u", password="p"), timeout=1)


def test_login_endpoint_down_does_not_switch_to_basic_auth():
    c = logged_in_client()
    _token_server(c, [Reply(503, None, "restarting"), Reply(200, {"AccessToken": "t"})])
    with pytest.raises(LspError, match="503"):
        c.get_all_channels()
    assert c.auth_mode != "basic"
    assert c.get_all_channels() == []  # recovers once login works again
    assert c.auth_mode == "token"


def test_login_refused_falls_back_to_basic_then_reprobes():
    c = logged_in_client()
    _token_server(c, [Reply(404, None, "no such route"), Reply(200, {"AccessToken": "t"})])
    with pytest.raises(LspError, match="both failed"):
        c.get_all_channels()
    assert c.auth_mode is None  # Basic was refused too: start over next time
    assert c.get_all_channels() == []
    assert c.auth_mode == "token"
