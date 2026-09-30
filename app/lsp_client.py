"""Client for the Telestream Live Schedule Pro v1 API.

Works out how the server wants to be authenticated on first use:
  1. no auth -- some servers (Basic auth provider) accept anonymous API calls;
  2. bearer token from /api/v1/auth/login, refreshed / re-obtained on 401;
  3. HTTP Basic auth header with the username and password.
Also handles channel lookup and creating, updating and removing events.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

import requests
import urllib3

from .config import LspConfig
from .models import ScheduledEvent

log = logging.getLogger(__name__)


class LspError(Exception):
    pass


class _LoginUnavailable(LspError):
    """The login endpoint couldn't be reached or failed (network, 5xx): says
    nothing about which auth the server wants, so it must not change it."""


class LspClient:
    def __init__(self, cfg: LspConfig, timeout: int = 30, read_only: bool = False):
        self.cfg = cfg
        # Dry run: refuse anything that would change LSP, as a last line of defence.
        self.read_only = read_only
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = cfg.verify_ssl
        if not cfg.verify_ssl:
            # Said once here rather than as a warning on every request, which
            # would fill the container log.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            log.warning("LSP TLS certificate checks are off (lsp.verify_ssl: false)")
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        # "none" | "token" | "basic"; None until the first request decides it.
        self.auth_mode: Optional[str] = None
        self._lock = threading.Lock()

    # -- auth ---------------------------------------------------------------

    def login(self) -> None:
        url = f"{self.cfg.base_url}/api/v1/auth/login"
        try:
            resp = self.session.post(
                url,
                json={"Username": self.cfg.username, "Password": self.cfg.password},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise _LoginUnavailable(f"Login request failed: {exc}") from exc
        if resp.status_code >= 500:
            raise _LoginUnavailable(f"Login failed ({resp.status_code}): {resp.text[:300]}")
        if resp.status_code != 200:
            raise LspError(f"Login failed ({resp.status_code}): {resp.text[:300]}")
        data = _json(resp, "Login")
        if not isinstance(data, dict):
            raise LspError("Login response missing AccessToken")
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
        try:
            data = resp.json()
        except ValueError:
            return False
        if isinstance(data, dict) and data.get("AccessToken"):
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
        try:
            return self.session.request(method, url, headers=headers, auth=auth, **kwargs)
        except requests.RequestException as exc:
            # Timeouts / connection errors are LSP failures like any other, so
            # callers that handle LspError (and save state) handle these too.
            raise LspError(f"{method} {url} failed: {exc}") from exc

    def _authenticate(self) -> None:
        """After a 401: get a token, or fall back to HTTP Basic. Caller holds _lock.

        Only a login the server actually refuses (4xx, or a 200 with no token)
        means "use Basic instead". A login endpoint that is down or erroring
        raises without changing the mode, so the next request tries again
        rather than sticking with Basic auth the server never wanted.
        """
        if not self._has_login():
            raise LspError(
                "LSP requires a login: set the LSP username & password "
                "(in the UI, or LSP_USERNAME/LSP_PASSWORD env)"
            )
        if self.auth_mode == "token" and self._refresh():
            return
        if self.auth_mode == "basic":
            # Basic was refused too: start over on the next request, in case
            # the server's auth (or the Basic decision) was only temporary.
            self.auth_mode = None
            raise LspError("LSP rejected the username/password (HTTP Basic auth)")
        try:
            self.login()
            self.auth_mode = "token"
        except _LoginUnavailable:
            raise
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
                self.auth_mode = None  # re-probe from scratch next time
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
        return _json(resp, "GetAllChannels")

    # -- settings -------------------------------------------------------------

    def get_general_settings(self) -> dict:
        """LSP's general settings (GET /api/v1/settings/general), e.g.
        EventCleanupThresholdInDays: how long LSP keeps an event after it ends."""
        resp = self._request("GET", "/api/v1/settings/general")
        if resp.status_code != 200:
            raise LspError(f"GetGeneralSettings failed ({resp.status_code}): {resp.text[:300]}")
        data = _json(resp, "GetGeneralSettings")
        return data if isinstance(data, dict) else {}

    # -- events -------------------------------------------------------------

    def get_events_for_channel(self, channel_id: str) -> list[dict]:
        """All events on a channel (used to match and check tracked events)."""
        resp = self._request(
            "GET", "/api/v1/GetEvents", params={"channelId": channel_id}
        )
        if resp.status_code != 200:
            raise LspError(
                f"GetEvents failed for {channel_id} ({resp.status_code}): {resp.text[:300]}"
            )
        return _json(resp, "GetEvents")

    def get_event(self, event_id: str) -> Optional[dict]:
        """One event exactly as LSP returns it (GetFilteredEvents), or None."""
        resp = self._request("GET", "/api/v1/GetFilteredEvents", params={"eventIds": event_id})
        if resp.status_code != 200:
            raise LspError(f"GetFilteredEvents failed for {event_id} ({resp.status_code}): "
                           f"{resp.text[:300]}")
        data = _json(resp, "GetFilteredEvents")
        items = data if isinstance(data, list) else [data]
        return next((e for e in items if isinstance(e, dict)
                     and str(e.get("Id") or "").lower() == event_id.lower()), None)

    def _check_writable(self, action: str, force: bool = False) -> None:
        # `force`: an explicit one-off operator action (Preview's per-event
        # send, cleanup) that is allowed while dry run is on.
        if self.read_only and not force:
            raise LspError(f"Dry run is on: refusing to {action} in LSP")

    def add_event(self, ev: ScheduledEvent, channel_id: str, name: str,
                  force: bool = False) -> dict:
        self._check_writable(f"create event {name!r}", force)
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
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def patch_event(self, event_id: str, name: str, start: datetime, end: datetime,
                    force: bool = False, extra: Optional[dict] = None) -> None:
        """Change an event that hasn't started (PATCH /api/v1/PatchEvent/{id}).
        `extra` adds fields, e.g. Customization / Labels to set a variable."""
        self._check_writable(f"update event {name!r}", force)
        body = {"Name": name, "Start": _iso(start), "End": _iso(end), **(extra or {})}
        resp = self._request("PATCH", f"/api/v1/PatchEvent/{event_id}", json=body)
        if resp.status_code != 200:
            raise LspError(
                f"PatchEvent failed for {name!r} ({resp.status_code}): {resp.text[:400]}"
            )
        try:
            kind = resp.json().get("ResultKind")
        except (ValueError, AttributeError):
            kind = None
        if kind and kind != "Success":
            raise LspError(f"PatchEvent for {name!r} returned {kind}: {resp.text[:400]}")

    def remove_event(self, event_id: str, force: bool = False) -> None:
        """Delete a single event by its LSP id (DELETE /api/v1/RemoveEvent)."""
        self._check_writable(f"delete event {event_id}", force)
        resp = self._request(
            "DELETE", "/api/v1/RemoveEvent", json={"EventId": event_id}
        )
        # 200/204 = removed; 404 = already gone, which we treat as success.
        if resp.status_code not in (200, 202, 204, 404):
            raise LspError(
                f"RemoveEvent failed for {event_id} ({resp.status_code}): {resp.text[:300]}"
            )


def _json(resp: requests.Response, what: str):
    try:
        return resp.json()
    except ValueError as exc:
        raise LspError(f"{what} returned a non-JSON reply: {resp.text[:300]}") from exc


def _iso(dt: datetime) -> str:
    """RFC3339 / ISO-8601 with offset, e.g. 2026-09-12T15:20:00-04:00."""
    return dt.isoformat()
