"""Client for the Telestream Live Schedule Pro v1 API.

Handles login, bearer-token auth with refresh/re-login on 401, channel lookup,
event de-duplication, and event creation.
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

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"} if self._access_token else {}

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make an authed request, transparently re-authing once on 401."""
        with self._lock:
            if not self._access_token:
                self.login()
        url = f"{self.cfg.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        resp = self.session.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            log.info("Access token rejected; refreshing / re-authenticating")
            with self._lock:
                if not self._refresh():
                    self.login()
            resp = self.session.request(method, url, headers=self._headers(), **kwargs)
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


def _iso(dt: datetime) -> str:
    """RFC3339 / ISO-8601 with offset, e.g. 2026-09-12T15:20:00-04:00."""
    return dt.isoformat()
