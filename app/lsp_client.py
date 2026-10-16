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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import LspConfig
from .models import ScheduledEvent

log = logging.getLogger(__name__)


class LspError(Exception):
    pass


# LSP's EventStatus values, in its enum order.
EVENT_STATUSES = ("Waiting", "Queued", "Recording", "Complete", "Failed", "Timeout", "Canceled")
# The statuses of events that haven't finished: all the sync needs to see.
CURRENT_STATUSES = ("Waiting", "Queued", "Recording", "Canceled")


def event_status(e: Optional[dict]) -> str:
    """An LSP event's status, lowercase ("" if unknown). LSP sends the
    EventStatus name; a number is taken as its position in the enum."""
    s = (e or {}).get("Status")
    if isinstance(s, int) and not isinstance(s, bool) and 0 <= s < len(EVENT_STATUSES):
        s = EVENT_STATUSES[s]
    s = str(s or "").strip().lower()
    return "canceled" if s == "cancelled" else s


def _retrying_adapter() -> HTTPAdapter:
    """Retries a request that couldn't connect, and a GET that timed out or
    got a 502/503/504 (LSP restarting, a network blip), a couple of times
    with a short backoff, instead of failing the whole pass. A POST, PATCH
    or DELETE that reached LSP is never retried here: it may have worked."""
    return HTTPAdapter(max_retries=Retry(
        total=2, connect=2, read=2, status=2, other=0, backoff_factor=1,
        status_forcelist=(502, 503, 504), allowed_methods=frozenset({"GET"}),
        raise_on_status=False, respect_retry_after_header=False))


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
        for prefix in ("http://", "https://"):
            self.session.mount(prefix, _retrying_adapter())
        # Seconds this machine's clock is ahead of LSP's (from its Date
        # header); None until a reply has one.
        self.clock_skew: Optional[float] = None
        # Whether GetFilteredEvents' status filter has been seen to work on
        # this server (None: not checked yet). See get_current_events().
        self._filter_ok: Optional[bool] = None
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
            resp = self.session.request(method, url, headers=headers, auth=auth, **kwargs)
        except requests.RequestException as exc:
            # Timeouts / connection errors are LSP failures like any other, so
            # callers that handle LspError (and save state) handle these too.
            raise LspError(f"{method} {url} failed: {exc}") from exc
        self._note_clock(resp)
        return resp

    def _note_clock(self, resp) -> None:
        try:
            server = parsedate_to_datetime((getattr(resp, "headers", None) or {}).get("Date"))
        except (TypeError, ValueError, IndexError):
            return
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
        self.clock_skew = (datetime.now(timezone.utc) - server).total_seconds()

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
        data = _json(resp, "GetAllChannels")
        if not isinstance(data, list):
            raise LspError(f"GetAllChannels returned {type(data).__name__}, not a list")
        return data

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
        """All events on a channel, finished ones included."""
        resp = self._request(
            "GET", "/api/v1/GetEvents", params={"channelId": channel_id}
        )
        if resp.status_code != 200:
            raise LspError(
                f"GetEvents failed for {channel_id} ({resp.status_code}): {resp.text[:300]}"
            )
        data = _json(resp, "GetEvents")
        if not isinstance(data, list):
            raise LspError(f"GetEvents for {channel_id} returned {type(data).__name__}, not a list")
        return data

    def get_current_events(self, channel_id: str) -> list[dict]:
        """The channel's events that haven't finished (CURRENT_STATUSES),
        which is all a sync needs: GetEvents returns every event LSP still
        keeps, a season's worth by spring.

        Uses GetFilteredEvents, but only once it is seen to work on this
        server: the first call also reads the whole channel and checks the
        filter returned exactly its unfinished events. Until then, or if a
        reply ever holds something it shouldn't, the whole channel is read
        (a filter that dropped live events would make them look deleted)."""
        if self._filter_ok is False:
            return self.get_events_for_channel(channel_id)
        resp = self._request("GET", "/api/v1/GetFilteredEvents",
                             params={"channelIds": [channel_id],
                                     "eventStatuses": list(CURRENT_STATUSES)})
        data = _json(resp, "GetFilteredEvents") if resp.status_code == 200 else None
        wanted = {s.lower() for s in CURRENT_STATUSES}
        if not isinstance(data, list) or any(
                not isinstance(e, dict) or event_status(e) not in wanted
                or str(e.get("ChannelId") or "").lower() != channel_id.lower() for e in data):
            self._filter_off("its reply didn't match the filter")
            return self.get_events_for_channel(channel_id)
        if self._filter_ok is None:
            full = self.get_events_for_channel(channel_id)
            expected = {e.get("Id") for e in full if event_status(e) in wanted}
            if {e.get("Id") for e in data} != expected:
                self._filter_off("it left out unfinished events")
                return full
            if expected:  # proven only once it has had something to return
                self._filter_ok = True
                log.info("LSP's GetFilteredEvents works; reading only unfinished events from now on")
            return full
        return data

    def _filter_off(self, why: str) -> None:
        if self._filter_ok is not False:
            log.warning("Not using LSP's GetFilteredEvents (%s); reading every event on each "
                        "channel instead", why)
        self._filter_ok = False

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
