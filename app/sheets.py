"""Read the per-sport schedule tabs from Google Sheets and parse events.

Layout is transposed / form-style: row headers run down `header_column`, and
each subsequent column is a single event. There is no single composite tab --
the schedule lives across several sport tabs (Football, Fall Olympic, ...),
each in this same shape, so we read every configured tab and concatenate.

Per tab we anchor on the DATE and EVENT rows to identify event columns, take
the FIRST `CONTROL ROOM` row as the recording channel (the main broadcast;
stacked secondary blocks like scoreboard feeds are ignored), and read the
game-start time from the GAME START / GAME TIME / START TIME row.

The grid -> events step is a pure function (`parse_grid`) so it can be tested
against a real workbook without hitting the Google API.
"""
from __future__ import annotations

import logging
import re
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta
from typing import Optional

from dateutil import parser as dateparser

from .config import Config
from .models import ScheduledEvent

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Canonical control rooms. The sheets use bare letters (A..E) on the current
# tabs and "CR1".."CR5" / "CR 3" on older ones; both name the same five rooms
# (A=CR1 ... E=CR5). We normalize everything to the letter.
_ROOM_LETTERS = ["A", "B", "C", "D", "E"]


class SheetReader:
    def __init__(self, cfg: Config):
        # Imported lazily so the pure parser (parse_grid) is usable without the
        # Google client libraries installed (e.g. in tests).
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        self.cfg = cfg
        creds = Credentials.from_service_account_file(
            cfg.google_credentials_file, scopes=SCOPES
        )
        # cache_discovery=False avoids a noisy warning and a file-cache dependency.
        self.service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    def read_events(self) -> list[ScheduledEvent]:
        events: list[ScheduledEvent] = []
        for tab in self.cfg.sheet.tabs:
            if not self._tab_enabled(tab):
                log.info("Tab %r is disabled; skipping", tab)
                continue
            try:
                grid = self._fetch_grid(tab)
                events.extend(parse_grid(tab, grid, self.cfg))
            except Exception:  # noqa: BLE001 - one bad tab shouldn't kill the run
                log.exception("Failed to read tab %r", tab)
        return events

    # -- Google fetch -------------------------------------------------------

    def _fetch_grid(self, tab: str) -> list[list[str]]:
        # FORMATTED_VALUE returns the strings as displayed in the sheet, which
        # is the most robust thing to parse (dates/times as the user sees them).
        resp = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.cfg.sheet.spreadsheet_id,
                range=f"'{tab}'",
                valueRenderOption="FORMATTED_VALUE",
                dateTimeRenderOption="FORMATTED_STRING",
            )
            .execute()
        )
        return resp.get("values", [])

    def _tab_enabled(self, tab: str) -> bool:
        override = self.cfg.tab_overrides.get(tab)
        return True if override is None else override.enabled


# ---------------------------------------------------------------------------
# Pure parsing (no I/O) -- tested directly against a real workbook.
# ---------------------------------------------------------------------------

def parse_grid(tab: str, grid: list[list[str]], cfg: Config) -> list[ScheduledEvent]:
    """Turn one tab's cell grid into schedulable events.

    `grid` is row-major (grid[row][col]); cells are strings (empty for blanks).
    """
    if not grid:
        log.warning("Tab %r is empty", tab)
        return []

    label_col = _col_letter_to_index(cfg.sheet.header_column)
    row_of = _locate_label_rows(grid, label_col, cfg)

    if row_of["event_name"] is None:
        log.error("Tab %r: no EVENT row found; skipping", tab)
        return []
    if row_of["datetime"] is None and (
        row_of["date"] is None or row_of["time"] is None
    ):
        log.error(
            "Tab %r: no start time row (need a datetime row, or both date and time)", tab
        )
        return []

    tab_override = cfg.tab_overrides.get(tab)
    default_room = tab_override.default_control_room if tab_override else None
    if row_of["pcr"] is None and not default_room:
        log.warning(
            "Tab %r: no CONTROL ROOM row and no default_control_room; "
            "events will need a manual room assignment", tab
        )

    n_cols = max(len(r) for r in grid)
    out: list[ScheduledEvent] = []
    seen: dict[tuple, int] = {}  # (name, date) -> count, for override disambiguation
    for col in range(label_col + 1, n_cols):
        ev = _parse_column(tab, grid, row_of, col, cfg, default_room, seen)
        if ev is not None:
            out.append(ev)

    out = _apply_event_overrides(tab, out, cfg)
    log.info("Tab %r: parsed %d schedulable event(s)", tab, len(out))
    return out


def _locate_label_rows(
    grid: list[list[str]], label_col: int, cfg: Config
) -> dict[str, Optional[int]]:
    """Map each field to the first grid row whose col-A header matches an alias.

    The FIRST match wins -- important for CONTROL ROOM, where a tab may repeat
    the row for a secondary (scoreboard) block we intentionally ignore.
    """
    labels = cfg.sheet.labels
    norm_wanted = {
        "event_name": [a.strip().lower() for a in labels.event_name],
        "pcr": [a.strip().lower() for a in labels.pcr],
        "date": [a.strip().lower() for a in labels.date],
        "time": [a.strip().lower() for a in labels.time],
        "datetime": [a.strip().lower() for a in labels.datetime],
    }
    found: dict[str, Optional[int]] = {f: None for f in norm_wanted}
    for r, row in enumerate(grid):
        if label_col >= len(row):
            continue
        header = str(row[label_col]).strip().lower()
        if not header:
            continue
        for field, aliases in norm_wanted.items():
            if found[field] is None and header in aliases:
                found[field] = r
    return found


def _cell(grid, row: Optional[int], col: int) -> str:
    if row is None or row >= len(grid) or col >= len(grid[row]):
        return ""
    return str(grid[row][col]).strip()


def _parse_column(
    tab, grid, row_of, col, cfg, default_room, seen
) -> Optional[ScheduledEvent]:
    name = _cell(grid, row_of.get("event_name"), col)
    if not name:
        return None  # empty column, not an event

    start = _parse_start(grid, row_of, col, cfg)
    if start is None:
        log.debug("Tab %r col %d (%s): no usable start time; skipping", tab, col + 1, name)
        return None

    room_raw = _cell(grid, row_of.get("pcr"), col) or (default_room or "")
    pcr = _normalize_room(room_raw)  # may be None -> unassigned (fix in UI)

    # A stable-ish source date string for override matching.
    date_str = _event_date_key(start)
    dedup_bucket = (name.strip().lower(), date_str)
    occurrence = seen.get(dedup_bucket, 0)
    seen[dedup_bucket] = occurrence + 1

    start = start - timedelta(minutes=cfg.scheduling.lead_in_minutes)
    end = start + timedelta(hours=cfg.scheduling.safety_cap_hours)
    return ScheduledEvent(
        name=name,
        pcr=pcr or "",
        start=start,
        end=end,
        source_tab=tab,
        source_column=col + 1,
        event_date=date_str,
        occurrence=occurrence,
    )


def _parse_start(grid, row_of, col, cfg) -> Optional[datetime]:
    skip = cfg.date_parsing.skip_values
    tz = cfg.sheet.timezone

    # Preferred: a single explicit datetime cell.
    dt_val = _cell(grid, row_of.get("datetime"), col)
    if dt_val:
        if dt_val.upper() in skip:
            return None
        return _parse_datetime_string(dt_val, tz, cfg)

    # Otherwise: separate date + time cells.
    date_val = _cell(grid, row_of.get("date"), col)
    time_val = _cell(grid, row_of.get("time"), col)
    if not date_val or not time_val:
        return None
    if date_val.upper() in skip or time_val.upper() in skip:
        return None
    # Time cells sometimes carry notes ("6:30 & 9:00 PM", "PRACTICE STARTS @2").
    # Take the first clean time token; if there isn't one, skip (not schedulable).
    time_val = _first_time_token(time_val)
    if not time_val:
        return None
    return _parse_datetime_string(f"{date_val} {time_val}", tz, cfg)


def _parse_datetime_string(text: str, tz, cfg) -> Optional[datetime]:
    try:
        dt, had_year = _parse_naive_with_year_flag(text)
    except (ValueError, OverflowError):
        log.warning("Could not parse date/time %r", text)
        return None
    if not had_year:
        dp = cfg.date_parsing
        year = dp.academic_year_start if dt.month >= dp.rollover_month else dp.academic_year_start + 1
        dt = dt.replace(year=year)
    return dt.replace(tzinfo=tz)


def _apply_event_overrides(tab, events, cfg) -> list[ScheduledEvent]:
    """Apply per-event UI overrides: ignore, control-room, or start-time fixes."""
    overrides = cfg.event_overrides.get(tab)
    if not overrides:
        return events
    out = []
    for ev in events:
        ov = overrides.get((ev.event_date, ev.name.strip().lower(), ev.occurrence))
        if ov is None:
            out.append(ev)
            continue
        if ov.ignore:
            log.debug("Tab %r: event %r on %s ignored by override", tab, ev.name, ev.event_date)
            continue
        if ov.control_room:
            room = _normalize_room(ov.control_room)
            if room:
                ev.pcr = room
        if ov.start is not None:
            ev.start = ov.start - timedelta(minutes=cfg.scheduling.lead_in_minutes)
            ev.end = ev.start + timedelta(hours=cfg.scheduling.safety_cap_hours)
        out.append(ev)
    return out


# -- small helpers ----------------------------------------------------------

def _event_date_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?\b")


def _first_time_token(text: str) -> Optional[str]:
    """Pull the first usable clock time out of a messy cell.

    "17:30:00" -> "17:30:00"; "6:30 & 9:00 PM" -> "6:30 PM"; free text with no
    time -> None. A bare hour like "@2" is treated as no usable time.
    """
    t = text.strip()
    # Fast path: looks like a plain HH:MM(:SS) or with AM/PM already.
    m = _TIME_RE.search(t)
    if not m:
        return None
    hour, minute, ampm = m.group(1), m.group(2), m.group(3)
    if minute is None and ampm is None:
        # Bare number (e.g. "@2") is too ambiguous to schedule on.
        return None
    minute = minute or "00"
    out = f"{hour}:{minute}"
    if ampm:
        out += f" {ampm.upper()}"
    return out


def _parse_naive_with_year_flag(text: str) -> tuple[datetime, bool]:
    """Parse a date/time string and report whether a year was actually present.

    dateutil fills a missing year from its `default`. We parse twice with
    different default years; if the result's year tracks the default, no year
    was in the string.
    """
    d1 = dateparser.parse(text, default=datetime(2001, 1, 1, 0, 0), fuzzy=True)
    d2 = dateparser.parse(text, default=datetime(2002, 1, 1, 0, 0), fuzzy=True)
    had_year = d1.year == d2.year
    return d1, had_year


def _normalize_room(value: str) -> Optional[str]:
    """Normalize a control-room cell to a canonical letter A..E.

    Accepts 'A', 'PCR A', 'Control Room B', 'CR1', 'CR 3', bare '2', etc.
    A=CR1 ... E=CR5. Returns None for blanks / 'N/A' / anything unrecognized.
    """
    if value is None:
        return None
    v = str(value).strip().upper()
    if not v or v in ("N/A", "NA", "TBD", "TBA", "-", "OFF", "NONE"):
        return None

    # "CR3" / "CR 3" / bare "3" -> room number -> letter.
    num = re.search(r"\bCR\s*([1-5])\b", v) or re.fullmatch(r"([1-5])", v)
    if num:
        return _ROOM_LETTERS[int(num.group(1)) - 1]

    # Prefer a standalone single-letter token (handles "PCR A", "CONTROL ROOM B").
    for tok in v.replace("-", " ").split():
        if len(tok) == 1 and tok in _ROOM_LETTERS:
            return tok
    # Last resort: a trailing A..E letter.
    letters = [c for c in v if c in _ROOM_LETTERS]
    return letters[-1] if letters else None


def _col_letter_to_index(letter: str) -> int:
    """'A' -> 0, 'B' -> 1, 'AA' -> 26 ..."""
    idx = 0
    for ch in letter.strip().upper():
        if not ch.isalpha():
            continue
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(idx - 1, 0)
