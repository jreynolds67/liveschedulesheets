"""Client for the Telestream Live Schedule Pro v1 API.

Works out how the server wants to be authenticated on first use:
  1. no auth -- some servers (Basic auth provider) accept anonymous API calls;
  2. bearer token from /api/v1/auth/login, refreshed / re-obtained on 401;
  3. HTTP Basic auth header with the username and password.
Also handles channel lookup, event de-duplication, and event creation.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

import requests

from .config import LspConfig
from .models import ScheduledEvent

log = logging.getLogger(__name__)


class LspError(Exception):
    pass


class LspClient:
    def __init__(self, cfg: LspConfig, timeout: int = 30):
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = cfg.verify_ssl
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        # "none" | "token" | "basic"; None until the first request decides it.
        self.auth_mode: Optional[str] = None
        self._lock = threading.Lock()

    # -- auth ---------------------------------------------------------------

    def login(self) -> None:
        url = f"{self.cfg.base_url}/api/v1/auth/login"
        resp = self.session.post(
            url,
            json={"Username": self.cfg.username, "Password": self.cfg.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise LspError(f"Login failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        self._access_token = data.get("AccessToken")
        self._refresh_token = data.get("RefreshToken")
        if not self._access_token:
            raise LspError("Login response missing AccessToken")
        log.info("Authenticated with Live Schedule Pro")

    def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        url = f"{self.cfg.base_url}/api/v1/auth/refresh"
        try:
            resp = self.session.post(
                url, json={"RefreshToken": self._refresh_token}, timeout=self.timeout
            )
        except requests.RequestException:
            return False
        if resp.status_code != 200:
            return False
        data = resp.json()
        if data.get("AccessToken"):
            self._access_token = data["AccessToken"]
            self._refresh_token = data.get("RefreshToken", self._refresh_token)
            log.debug("Refreshed access token")
            return True
        return False

    def _has_login(self) -> bool:
        return bool(self.cfg.username and self.cfg.password)

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        headers, auth = {}, None
        if self.auth_mode == "token" and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        elif self.auth_mode == "basic":
            auth = (self.cfg.username, self.cfg.password)
        return self.session.request(method, url, headers=headers, auth=auth, **kwargs)

    def _authenticate(self) -> None:
        """After a 401: get a token, or fall back to HTTP Basic. Caller holds _lock."""
        if not self._has_login():
            raise LspError(
                "LSP requires a login: set the LSP username & password "
                "(in the UI, or LSP_USERNAME/LSP_PASSWORD env)"
            )
        if self.auth_mode == "token" and self._refresh():
            return
        if self.auth_mode == "basic":
            raise LspError("LSP rejected the username/password (HTTP Basic auth)")
        try:
            self.login()
            self.auth_mode = "token"
        except LspError as exc:
            log.info("Token login failed (%s); trying HTTP Basic auth", exc)
            self.auth_mode = "basic"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make a request, (re)authenticating as needed on 401."""
        url = f"{self.cfg.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        resp = self._send(method, url, **kwargs)
        if resp.status_code == 401:
            with self._lock:
                self._authenticate()
            resp = self._send(method, url, **kwargs)
            if resp.status_code == 401 and self.auth_mode == "basic":
                raise LspError(
                    "LSP rejected the login: token login and HTTP Basic auth both failed "
                    "-- check the username & password"
                )
        if resp.status_code != 401 and self.auth_mode is None:
            self.auth_mode = "none"
            log.info("LSP accepted API calls without a login")
        return resp

    # -- channels -----------------------------------------------------------

    def get_all_channels(self) -> list[dict]:
        resp = self._request("GET", "/api/v1/GetAllChannels")
        if resp.status_code != 200:
            raise LspError(f"GetAllChannels failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    # -- events -------------------------------------------------------------

    def get_events_for_channel(self, channel_id: str) -> list[dict]:
        """All events on a channel (used for de-duplication)."""
        resp = self._request(
            "GET", "/api/v1/GetEvents", params={"channelId": channel_id}
        )
        if resp.status_code != 200:
            raise LspError(
                f"GetEvents failed for {channel_id} ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()

    def add_event(self, ev: ScheduledEvent, channel_id: str, name: str) -> dict:
        body = {
            "Name": name,
            "Start": _iso(ev.start),
            "End": _iso(ev.end),
            "ChannelId": channel_id,
        }
        resp = self._request("POST", "/api/v1/AddEvent", json=body)
        if resp.status_code != 200:
            raise LspError(
                f"AddEvent failed for {name!r} ({resp.status_code}): {resp.text[:400]}"
            )
        return resp.json()

    def remove_event(self, event_id: str) -> None:
        """Delete a single event by its LSP id (DELETE /api/v1/RemoveEvent)."""
        resp = self._request(
            "DELETE", "/api/v1/RemoveEvent", json={"EventId": event_id}
        )
        # 200/204 = removed; 404 = already gone, which we treat as success.
        if resp.status_code not in (200, 202, 204, 404):
            raise LspError(
                f"RemoveEvent failed for {event_id} ({resp.status_code}): {resp.text[:300]}"
            )


def _iso(dt: datetime) -> str:
    """RFC3339 / ISO-8601 with offset, e.g. 2026-09-12T15:20:00-04:00."""
    return dt.isoformat()
