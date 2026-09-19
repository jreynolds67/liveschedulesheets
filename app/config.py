"""Configuration model + loading/parsing/saving.

Operational settings live in a writable YAML file (on the /data volume so the
web UI can edit them). Secrets may come from that file OR from environment
variables; the file takes precedence when set, env is the fallback.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class LabelMap:
    event_name: list[str]
    pcr: list[str]
    date: list[str]
    time: list[str]
    datetime: list[str]


@dataclass
class SheetConfig:
    spreadsheet_id: str
    tabs: list[str]
    header_column: str
    labels: LabelMap
    timezone: ZoneInfo


@dataclass
class DateParsingConfig:
    academic_year_start: int
    rollover_month: int
    skip_values: set[str]


@dataclass
class SchedulingConfig:
    lead_in_minutes: int
    safety_cap_hours: float
    horizon_days: int
    past_grace_minutes: int
    event_name_prefix: str


@dataclass
class ChannelRef:
    channel_name: Optional[str] = None
    channel_id: Optional[str] = None


@dataclass
class TabOverride:
    """Per-tab UI settings: enable/ignore a whole tab, or supply a default room
    for tabs that have no CONTROL ROOM row (e.g. Football)."""
    enabled: bool = True
    default_control_room: Optional[str] = None


@dataclass
class EventOverride:
    """Per-event UI fix, matched by (date, event-name, occurrence)."""
    control_room: Optional[str] = None
    start: Optional[datetime] = None
    ignore: bool = False


@dataclass
class LspConfig:
    base_url: str
    verify_ssl: bool
    username: str
    password: str


@dataclass
class RuntimeConfig:
    poll_interval_seconds: int
    run_once: bool
    dry_run: bool
    state_file: str
    log_level: str


@dataclass
class Config:
    sheet: SheetConfig
    date_parsing: DateParsingConfig
    scheduling: SchedulingConfig
    pcr_channel_map: dict[str, ChannelRef]
    lsp: LspConfig
    runtime: RuntimeConfig
    google_credentials_file: str
    tab_overrides: dict[str, TabOverride] = field(default_factory=dict)
    # tab -> {(date, name_lower, occurrence): EventOverride}
    event_overrides: dict[str, dict[tuple, EventOverride]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------

def load_raw(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def save_raw(path: str, raw: dict) -> None:
    import tempfile

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)


def load_config(path: str = "config.yaml") -> Config:
    return parse_config(load_raw(path))


# --------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------

def parse_config(raw: dict) -> Config:
    """Validate a raw config dict into a Config. Secrets fall back to env."""
    raw = raw or {}

    sheet_raw = _require(raw, "sheet", "root")
    labels_raw = _require(sheet_raw, "labels", "sheet")
    labels = LabelMap(
        event_name=_as_list(labels_raw.get("event_name")),
        pcr=_as_list(labels_raw.get("pcr")),
        date=_as_list(labels_raw.get("date")),
        time=_as_list(labels_raw.get("time")),
        datetime=_as_list(labels_raw.get("datetime")),
    )
    if not labels.event_name:
        raise ConfigError("sheet.labels.event_name must list at least one label")
    if not labels.pcr:
        raise ConfigError("sheet.labels.pcr must list at least one label")
    if not labels.datetime and not (labels.date and labels.time):
        raise ConfigError(
            "Provide sheet.labels.datetime, OR both sheet.labels.date and sheet.labels.time"
        )

    tz_name = sheet_raw.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown timezone '{tz_name}'") from exc

    tabs = _as_list(sheet_raw.get("tabs"))
    if not tabs:
        raise ConfigError("sheet.tabs must list at least one worksheet tab")

    sheet = SheetConfig(
        spreadsheet_id=_require(sheet_raw, "spreadsheet_id", "sheet"),
        tabs=tabs,
        header_column=str(sheet_raw.get("header_column", "A")).strip().upper(),
        labels=labels,
        timezone=tz,
    )

    dp_raw = raw.get("date_parsing", {}) or {}
    date_parsing = DateParsingConfig(
        academic_year_start=int(dp_raw.get("academic_year_start", 0)) or _current_academic_year(),
        rollover_month=int(dp_raw.get("rollover_month", 8)),
        skip_values={s.strip().upper() for s in _as_list(dp_raw.get("skip_values")) if s.strip()},
    )

    sc_raw = raw.get("scheduling", {}) or {}
    scheduling = SchedulingConfig(
        lead_in_minutes=int(sc_raw.get("lead_in_minutes", 0)),
        safety_cap_hours=float(sc_raw.get("safety_cap_hours", 6)),
        horizon_days=int(sc_raw.get("horizon_days", 60)),
        past_grace_minutes=int(sc_raw.get("past_grace_minutes", 30)),
        event_name_prefix=str(sc_raw.get("event_name_prefix", "")),
    )

    pcr_map_raw = raw.get("pcr_channel_map", {}) or {}
    pcr_channel_map: dict[str, ChannelRef] = {}
    for letter, ref in pcr_map_raw.items():
        ref = ref or {}
        pcr_channel_map[str(letter).strip().upper()] = ChannelRef(
            channel_name=(ref.get("channel_name") or None),
            channel_id=(ref.get("channel_id") or None),
        )
    if not pcr_channel_map:
        raise ConfigError("pcr_channel_map must map at least one PCR letter to a channel")

    lsp_raw = _require(raw, "lsp", "root")
    username = (lsp_raw.get("username") or os.environ.get("LSP_USERNAME", "")).strip()
    password = lsp_raw.get("password") or os.environ.get("LSP_PASSWORD", "")
    if not username or not password:
        raise ConfigError("Set LSP username & password (in the UI, or LSP_USERNAME/LSP_PASSWORD env)")
    lsp = LspConfig(
        base_url=str(_require(lsp_raw, "base_url", "lsp")).rstrip("/"),
        verify_ssl=bool(lsp_raw.get("verify_ssl", True)),
        username=username,
        password=password,
    )

    rt_raw = raw.get("runtime", {}) or {}
    runtime = RuntimeConfig(
        poll_interval_seconds=int(rt_raw.get("poll_interval_seconds", 900)),
        run_once=_env_bool("RUN_ONCE", bool(rt_raw.get("run_once", False))),
        dry_run=_env_bool("DRY_RUN", bool(rt_raw.get("dry_run", False))),
        state_file=str(rt_raw.get("state_file", "/data/state.json")),
        log_level=os.environ.get("LOG_LEVEL", str(rt_raw.get("log_level", "INFO"))).upper(),
    )

    google_raw = raw.get("google", {}) or {}
    google_creds = (
        google_raw.get("credentials_file")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    ).strip()
    if not google_creds:
        raise ConfigError(
            "Set the Google service-account key path (GOOGLE_APPLICATION_CREDENTIALS env "
            "or google.credentials_file in config)"
        )
    if not os.path.exists(google_creds):
        raise ConfigError(f"Google credentials file not found: {google_creds}")

    tab_overrides = _parse_tab_overrides(raw.get("tab_overrides", {}) or {})
    event_overrides = _parse_event_overrides(raw.get("event_overrides", {}) or {}, tz)

    return Config(
        sheet=sheet,
        date_parsing=date_parsing,
        scheduling=scheduling,
        pcr_channel_map=pcr_channel_map,
        lsp=lsp,
        runtime=runtime,
        google_credentials_file=google_creds,
        tab_overrides=tab_overrides,
        event_overrides=event_overrides,
    )


def _parse_tab_overrides(raw: dict) -> dict[str, TabOverride]:
    out: dict[str, TabOverride] = {}
    for tab, cfg in (raw or {}).items():
        cfg = cfg or {}
        out[str(tab)] = TabOverride(
            enabled=bool(cfg.get("enabled", True)),
            default_control_room=(cfg.get("default_control_room") or None),
        )
    return out


def _parse_event_overrides(raw: dict, tz: ZoneInfo) -> dict[str, dict[tuple, EventOverride]]:
    """raw: {tab: [ {date, event, occurrence?, control_room?, start?, ignore?}, ... ]}"""
    out: dict[str, dict[tuple, EventOverride]] = {}
    for tab, items in (raw or {}).items():
        bucket: dict[tuple, EventOverride] = {}
        for item in items or []:
            item = item or {}
            date = str(item.get("date", "")).strip()
            name = str(item.get("event", "")).strip().lower()
            if not date or not name:
                continue  # need both to match an event
            occ = int(item.get("occurrence", 0))
            start = None
            start_raw = item.get("start")
            if start_raw:
                try:
                    start = datetime.fromisoformat(str(start_raw)).replace(tzinfo=tz)
                except ValueError:
                    pass
            bucket[(date, name, occ)] = EventOverride(
                control_room=(item.get("control_room") or None),
                start=start,
                ignore=bool(item.get("ignore", False)),
            )
        if bucket:
            out[str(tab)] = bucket
    return out


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _require(d: dict, key: str, ctx: str):
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"Missing required config '{key}' under {ctx}")
    return d[key]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _current_academic_year() -> int:
    from datetime import datetime

    now = datetime.now()
    return now.year if now.month >= 8 else now.year - 1
