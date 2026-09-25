"""Configuration model + loading/parsing/saving.

Operational settings live in a writable YAML file (on the /data volume so the
web UI can edit them). Secrets may come from that file OR from environment
variables; the file takes precedence when set, env is the fallback.
"""
from __future__ import annotations

import os
import re
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
    """Which LSP channels a control room records on: every channel whose name
    contains `match` as a whole word (case-insensitive), resolved live each
    pass, plus `channel_id` if pinned."""
    match: Optional[str] = None
    channel_id: Optional[str] = None

    def matches(self, channel_name: str) -> bool:
        return bool(self.match) and channel_name_matches(self.match, channel_name)


def channel_name_matches(match: str, channel_name: str) -> bool:
    """True if `match` appears in `channel_name` as whole words, ignoring case
    and spacing: "PCR A" matches "01 - PCR A PGM (x264)" but not "PCR AB"."""
    words = [re.escape(w) for w in match.split()]
    if not words:
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"\s*".join(words) + r"(?![A-Za-z0-9])"
    return re.search(pattern, channel_name or "", re.IGNORECASE) is not None


# Fields a tab's rows can be mapped to (keys of LabelMap / TabOverride.rows).
ROW_FIELDS = ("event_name", "date", "time", "pcr", "datetime")


@dataclass
class RowPick:
    """An operator's manual choice of which sheet row feeds a field.

    `row` is 1-based as shown in Sheets; `label` is that row's column-A header
    at pick time, used to follow the row if rows are later inserted/removed.
    `none` explicitly means "this tab has no such row" (skip auto-detection).
    """
    row: Optional[int] = None
    label: str = ""
    none: bool = False


@dataclass
class TabOverride:
    """Per-tab UI settings: enable/ignore a whole tab, supply a default room
    for tabs that have no CONTROL ROOM row (e.g. Football), and pin which row
    feeds each field when auto-detection gets it wrong."""
    enabled: bool = True
    default_control_room: Optional[str] = None
    rows: dict[str, RowPick] = field(default_factory=dict)


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
class SheetSettings:
    """The part of Config needed to read and parse the sheet (no LSP). Has the
    same attribute names as Config, so sheet code accepts either."""
    sheet: SheetConfig
    date_parsing: DateParsingConfig
    scheduling: SchedulingConfig
    google_credentials_file: str
    tab_overrides: dict[str, TabOverride] = field(default_factory=dict)
    event_overrides: dict[str, dict[tuple, EventOverride]] = field(default_factory=dict)


@dataclass
class LspSettings:
    """The part of Config needed to talk to LSP (no sheet / Google key), so the
    LSP side of the UI works before the Google key is installed."""
    pcr_channel_map: dict[str, ChannelRef]
    lsp: LspConfig
    runtime: RuntimeConfig


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

def parse_sheet_settings(raw: dict) -> SheetSettings:
    """Validate just the sheet-reading half of the config.

    Used on its own by the web UI's sheet preview, which must work before the
    LSP login is filled in.
    """
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

    # Empty tabs => auto-discover all VISIBLE tabs at runtime (hidden tabs like
    # COUNT / the stale *RELAYOUT composites are always skipped).
    tabs = _as_list(sheet_raw.get("tabs"))

    spreadsheet_id = extract_spreadsheet_id(str(_require(sheet_raw, "spreadsheet_id", "sheet")))
    if not spreadsheet_id:
        raise ConfigError("sheet.spreadsheet_id is not a valid Google Sheets link or ID")

    sheet = SheetConfig(
        spreadsheet_id=spreadsheet_id,
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
    if os.path.isdir(google_creds):
        # Docker creates an empty directory when a bind-mounted file is missing on the host.
        raise ConfigError(
            f"Google credentials path {google_creds} is a directory, not a key file -- "
            "the key file probably isn't at the host path mounted in docker-compose.yml"
        )
    if not os.path.isfile(google_creds):
        raise ConfigError(
            f"Google credentials file not found: {google_creds} (on the Docker host, put "
            "service-account.json in the folder mounted at /secrets)"
        )

    return SheetSettings(
        sheet=sheet,
        date_parsing=date_parsing,
        scheduling=scheduling,
        google_credentials_file=google_creds,
        tab_overrides=_parse_tab_overrides(raw.get("tab_overrides", {}) or {}),
        event_overrides=_parse_event_overrides(raw.get("event_overrides", {}) or {}, tz),
    )


def parse_config(raw: dict) -> Config:
    """Validate a raw config dict into a Config. Secrets fall back to env."""
    raw = raw or {}
    ss = parse_sheet_settings(raw)
    ls = parse_lsp_settings(raw)
    return Config(
        sheet=ss.sheet,
        date_parsing=ss.date_parsing,
        scheduling=ss.scheduling,
        pcr_channel_map=ls.pcr_channel_map,
        lsp=ls.lsp,
        runtime=ls.runtime,
        google_credentials_file=ss.google_credentials_file,
        tab_overrides=ss.tab_overrides,
        event_overrides=ss.event_overrides,
    )


def parse_lsp_settings(raw: dict) -> LspSettings:
    """Validate just the LSP half of the config (login, PCR map, runtime)."""
    raw = raw or {}
    pcr_map_raw = raw.get("pcr_channel_map", {}) or {}
    pcr_channel_map: dict[str, ChannelRef] = {}
    for letter, ref in pcr_map_raw.items():
        ref = ref or {}
        letter = str(letter).strip().upper()
        pcr_channel_map[letter] = ChannelRef(
            # channel_name is the older name for this field.
            match=(ref.get("channel_match") or ref.get("channel_name") or None),
            channel_id=(ref.get("channel_id") or None),
        )
    if not pcr_channel_map:
        raise ConfigError("pcr_channel_map must map at least one PCR letter to channels")

    lsp_raw = _require(raw, "lsp", "root")
    username = (lsp_raw.get("username") or os.environ.get("LSP_USERNAME", "")).strip()
    password = lsp_raw.get("password") or os.environ.get("LSP_PASSWORD", "")
    # Login is optional: some LSP servers (Basic auth provider) accept API calls
    # without one. The client works out what the server needs (see lsp_client).
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
        # Dry run unless the UI's toggle has explicitly gone live (runtime.live).
        # The older runtime.dry_run key is ignored so existing configs start safe.
        # DRY_RUN env overrides (headless / docker run testing).
        dry_run=_env_bool("DRY_RUN", not bool(rt_raw.get("live", False))),
        state_file=str(rt_raw.get("state_file", "/data/state.json")),
        log_level=os.environ.get("LOG_LEVEL", str(rt_raw.get("log_level", "INFO"))).upper(),
    )

    return LspSettings(pcr_channel_map=pcr_channel_map, lsp=lsp, runtime=runtime)


def _parse_tab_overrides(raw: dict) -> dict[str, TabOverride]:
    out: dict[str, TabOverride] = {}
    for tab, cfg in (raw or {}).items():
        cfg = cfg or {}
        out[str(tab)] = TabOverride(
            enabled=bool(cfg.get("enabled", True)),
            default_control_room=(cfg.get("default_control_room") or None),
            rows=parse_row_picks(cfg.get("rows")),
        )
    return out


def parse_row_picks(raw) -> dict[str, RowPick]:
    """raw: {field: {row: 7, label: "CONTROL ROOM"} | {none: true}}"""
    out: dict[str, RowPick] = {}
    for fld, pick in (raw or {}).items():
        if fld not in ROW_FIELDS or not isinstance(pick, dict):
            continue
        if pick.get("none"):
            out[fld] = RowPick(none=True)
            continue
        try:
            row = int(pick.get("row"))
        except (TypeError, ValueError):
            continue
        if row >= 1:
            out[fld] = RowPick(row=row, label=str(pick.get("label") or "").strip())
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

_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def extract_spreadsheet_id(text: str) -> str:
    """Accept a full Google Sheets link or a bare ID; return the ID ('' if neither).

    https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0 -> <ID>
    """
    t = (text or "").strip()
    m = _SHEET_URL_RE.search(t)
    if m:
        return m.group(1)
    return t if _SHEET_ID_RE.match(t) else ""


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
