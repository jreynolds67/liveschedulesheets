"""Read the per-sport schedule tabs from Google Sheets and parse events.

Layout is transposed / form-style: row headers run down `header_column`, and
each subsequent column is a single event. There is no single composite tab --
the schedule lives across several sport tabs (Football, Fall Olympic, ...),
each in this same shape, so we read every configured tab and concatenate.

Per tab we find the DATE, EVENT, start-time and PCR (CONTROL ROOM) rows
(`locate_rows`): an operator's manual pick from the web UI wins, then an exact
configured label, then a header keyword, then (dates / room letters only) the
row's contents. For labels the FIRST match wins, so the main broadcast's
CONTROL ROOM row is used and stacked secondary blocks (scoreboard feeds) are
ignored.

The grid -> events step is a pure function (`parse_grid`) so it can be tested
against a real workbook without hitting the Google API; `inspect_grid` builds
the web UI's sheet preview the same way.
"""
from __future__ import annotations

import logging
import re
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta
from dataclasses import dataclass
from typing import Optional

from dateutil import parser as dateparser

from .config import ROW_FIELDS, Config
from .models import ScheduledEvent

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Canonical control rooms (PCRs). The PCR / CONTROL ROOM row holds a letter,
# bare ("A") or prefixed ("PCR A").
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
        for tab in self._tabs_to_read():
            if not self._tab_enabled(tab):
                log.info("Tab %r is disabled; skipping", tab)
                continue
            try:
                grid = self.fetch_grid(tab)
                events.extend(parse_grid(tab, grid, self.cfg))
            except Exception:  # noqa: BLE001 - one bad tab shouldn't kill the run
                log.exception("Failed to read tab %r", tab)
        return events

    def _tabs_to_read(self) -> list[str]:
        """Which tabs to parse: the visible tabs, minus hidden ones.

        Hidden tabs (COUNT, the stale *RELAYOUT composites) are skipped
        automatically. If `sheet.tabs` is configured, it is used as an explicit
        allow-list but hidden tabs are still dropped; if it is empty, every
        visible tab is read (auto-discovery).
        """
        try:
            visible = self.visible_tabs()
        except Exception:  # noqa: BLE001 - metadata call is best-effort
            log.exception("Could not read tab metadata; falling back to configured tabs")
            return list(self.cfg.sheet.tabs)

        configured = list(self.cfg.sheet.tabs)
        if not configured:
            return visible  # auto-discover: all visible tabs

        visible_set = set(visible)
        result = []
        for tab in configured:
            if tab in visible_set:
                result.append(tab)
            else:
                log.info("Tab %r is hidden or missing; skipping", tab)
        return result

    # -- Google fetch -------------------------------------------------------

    def sheet_meta(self) -> dict:
        """{"title", "tabs": [{"name", "gid", "hidden"}, ...]} with tabs in order.
        `gid` matches the #gid=... in a Sheets link, so the UI can open the tab
        a pasted link points at."""
        resp = (
            self.service.spreadsheets()
            .get(
                spreadsheetId=self.cfg.sheet.spreadsheet_id,
                fields="properties.title,sheets.properties(sheetId,title,hidden,index)",
            )
            .execute()
        )
        sheets = sorted(
            (s.get("properties", {}) for s in resp.get("sheets", [])),
            key=lambda p: p.get("index", 0),
        )
        return {
            "title": (resp.get("properties") or {}).get("title", ""),
            "tabs": [
                {"name": p["title"], "gid": p.get("sheetId"), "hidden": bool(p.get("hidden"))}
                for p in sheets if p.get("title")
            ],
        }

    def visible_tabs(self) -> list[str]:
        return [t["name"] for t in self.sheet_meta()["tabs"] if not t["hidden"]]

    def fetch_grid(self, tab: str) -> list[list[str]]:
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
    tab_override = cfg.tab_overrides.get(tab)
    picks = tab_override.rows if tab_override else {}
    matches = locate_rows(grid, label_col, cfg.sheet.labels, picks)
    row_of = {fld: m.row for fld, m in matches.items()}
    for fld, m in matches.items():
        if m.method in ("keyword", "content", "moved", "stale"):
            log.info("Tab %r: %s -> row %s (%s)", tab, fld,
                     "none" if m.row is None else m.row + 1, m.note or m.method)

    if row_of["event_name"] is None:
        log.error("Tab %r: no EVENT row found; skipping", tab)
        return []
    if row_of["date"] is None or row_of["time"] is None:
        log.error("Tab %r: no DATE and start-time rows found; skipping", tab)
        return []

    if row_of["pcr"] is None:
        log.warning("Tab %r: no PCR row; events will need a PCR assigned", tab)

    n_cols = max(len(r) for r in grid)
    out: list[ScheduledEvent] = []
    seen: dict[tuple, int] = {}  # (name, date) -> count, for override disambiguation
    for col in range(label_col + 1, n_cols):
        ev = _parse_column(tab, grid, row_of, col, cfg, seen)
        if ev is not None:
            out.append(ev)

    out = _apply_event_overrides(tab, out, cfg)
    log.info("Tab %r: parsed %d schedulable event(s)", tab, len(out))
    return out


# ---------------------------------------------------------------------------
# Row detection: which row feeds each field.
#
# Tried in order, and a row claimed by an earlier step is never reused:
#   1. manual  - the operator's pick from the web UI (tab_overrides.rows)
#   2. label   - column-A header exactly matches a configured label
#   3. keyword - header contains a telling word ("KICKOFF", "Ctrl Room" ...)
#   4. content - the row's cells look right (dates, or room letters A..E)
# There is deliberately no content fallback for the start TIME: CREW CALL,
# AUDIO CHECK and DOORS rows are full of times too, so guessing is unsafe.
# ---------------------------------------------------------------------------

@dataclass
class RowMatch:
    row: Optional[int]  # 0-based grid row; None = not found
    method: str = ""    # manual | moved | stale | label | keyword | content | none | ""
    note: str = ""

    def to_dict(self, grid, label_col) -> dict:
        return {
            "row": None if self.row is None else self.row + 1,
            "label": "" if self.row is None else _cell(grid, self.row, label_col),
            "method": self.method,
            "note": self.note,
        }


# field -> (keywords in priority order, words that disqualify a header)
_KEYWORDS: dict[str, tuple[list[str], list[str]]] = {
    "event_name": (["event", "matchup", "title"], ["other"]),
    "date": (["date"], ["update"]),
    "time": (
        ["start", "kickoff", "kick off", "game time", "tip", "first pitch",
         "puck drop", "air time", "time"],
        ["call", "check", "door", "end", "meal", "run thru", "crew", "arrival"],
    ),
    "pcr": (["control room", "pcr", "ctrl room"], ["shadow"]),
}

_DATE_RE = re.compile(
    r"\b\d{1,2}/\d{1,2}\b|\b\d{4}-\d{1,2}-\d{1,2}\b|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_ROOM_CELL_RE = re.compile(r"^(?:(?:PCR|CONTROL ROOM|CR)\s*)?[A-E]$")
_CONTENT_CHECKS = {
    "date": lambda v: bool(_DATE_RE.search(v)),
    "pcr": lambda v: bool(_ROOM_CELL_RE.match(v.strip().upper())),
}


def locate_rows(
    grid: list[list[str]], label_col: int, labels, picks: Optional[dict] = None
) -> dict[str, RowMatch]:
    """Decide which grid row feeds each field (see the notes above)."""
    picks = picks or {}
    headers = [_cell(grid, r, label_col).lower() for r in range(len(grid))]
    out: dict[str, RowMatch] = {}
    claimed: set[int] = set()

    def take(fld: str, row: Optional[int], method: str, note: str = "") -> None:
        out[fld] = RowMatch(row, method, note)
        if row is not None:
            claimed.add(row)

    # 1. manual picks
    for fld in ROW_FIELDS:
        pick = picks.get(fld)
        if pick is None:
            continue
        if pick.none:
            take(fld, None, "none", "Set to 'no row' by operator")
        else:
            take(fld, *_resolve_pick(headers, pick))

    # 2. exact configured labels (FIRST match wins -- important for CONTROL
    #    ROOM, where a tab may repeat the row for a secondary scoreboard block)
    for fld in ROW_FIELDS:
        if fld in out:
            continue
        aliases = {a.strip().lower() for a in getattr(labels, fld)}
        for r, h in enumerate(headers):
            if h and h in aliases and r not in claimed:
                take(fld, r, "label")
                break

    # 3. keyword match on the header text. Headers naming only this field win
    #    over ambiguous ones ("Event Name" beats "Event Date" for the event).
    hits = {r: _keyword_fields(h) for r, h in enumerate(headers) if h}
    for strict in (True, False):
        for fld in ROW_FIELDS:
            if fld in out:
                continue
            best = None
            for r, found in hits.items():
                if r in claimed or fld not in found or (strict and len(found) > 1):
                    continue
                if best is None or found[fld][0] < best[1]:
                    best = (r, found[fld][0], found[fld][1])
            if best is not None:
                take(fld, best[0], "keyword", f"Header contains '{best[2]}'")

    # 4. content (dates / room letters only)
    for fld, check in _CONTENT_CHECKS.items():
        if fld in out:
            continue
        best, best_hits = None, 0
        for r, row in enumerate(grid):
            if r in claimed:
                continue
            vals = [str(v).strip() for c, v in enumerate(row) if c > label_col and str(v).strip()]
            hits = sum(1 for v in vals if check(v))
            if hits >= 2 and hits >= 0.7 * len(vals) and hits > best_hits:
                best, best_hits = r, hits
        if best is not None:
            take(fld, best, "content", f"{best_hits} cells look like a {fld.replace('pcr', 'room')}")

    for fld in ROW_FIELDS:
        out.setdefault(fld, RowMatch(None))
    return out


def _keyword_fields(header: str) -> dict[str, tuple[int, str]]:
    """{field: (keyword priority, keyword)} for every field this header names."""
    found = {}
    for fld, (keywords, excluded) in _KEYWORDS.items():
        if any(x in header for x in excluded):
            continue
        for i, kw in enumerate(keywords):
            if re.search(rf"\b{re.escape(kw)}\b", header):
                found[fld] = (i, kw)
                break
    return found


# How far a manual pick will follow its label after rows are inserted/removed.
# Beyond this, a same-named row is more likely a different block (e.g. the
# SCOREBOARD section repeats CREW CALL / CONTROL ROOM) than the same row moved.
_PICK_FOLLOW_ROWS = 15


def _resolve_pick(headers: list[str], pick) -> tuple[Optional[int], str, str]:
    """Follow a manual pick to its current row.

    If the header at the picked row still matches, use it. If rows moved, use
    the nearest row carrying the same header (within _PICK_FOLLOW_ROWS). If the
    header is gone, fall back to the row number and flag it as stale.
    """
    row = pick.row - 1
    want = pick.label.strip().lower()
    here = headers[row] if row < len(headers) else ""
    if not want or here == want:
        return (row if row < len(headers) else None), "manual", ""
    same = [r for r, h in enumerate(headers)
            if h == want and abs(r - row) <= _PICK_FOLLOW_ROWS]
    if same:
        moved = min(same, key=lambda r: abs(r - row))
        return moved, "moved", f"Row moved from {pick.row} to {moved + 1}"
    note = f"'{pick.label}' is no longer at or near row {pick.row}; using row {pick.row} as-is"
    log.warning("Row pick: %s", note)
    return (row if row < len(headers) else None), "stale", note


def _cell(grid, row: Optional[int], col: int) -> str:
    if row is None or row >= len(grid) or col >= len(grid[row]):
        return ""
    return str(grid[row][col]).strip()


def _parse_column(tab, grid, row_of, col, cfg, seen) -> Optional[ScheduledEvent]:
    name = _cell(grid, row_of.get("event_name"), col)
    if not name:
        return None  # empty column, not an event

    start = _parse_start(grid, row_of, col, cfg)
    if start is None:
        log.debug("Tab %r col %d (%s): no usable start time; skipping", tab, col + 1, name)
        return None

    pcr = _normalize_room(_cell(grid, row_of.get("pcr"), col))  # None -> unassigned (fix in UI)

    # A stable-ish source date string for override matching.
    date_str = _event_date_key(start)
    dedup_bucket = (name.strip().lower(), date_str)
    occurrence = seen.get(dedup_bucket, 0)
    seen[dedup_bucket] = occurrence + 1

    start = start - timedelta(minutes=cfg.scheduling.lead_in_minutes)
    end = start + timedelta(hours=cfg.scheduling.cap_hours_for(name))
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

    # Date and start time always live in separate rows.
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
        # Dates must carry their year in the sheet (e.g. 9/12/2026); guessing
        # it is how events end up a year out.
        log.warning("Date/time %r has no year; skipping", text)
        return None
    return dt.replace(tzinfo=tz)


# ---------------------------------------------------------------------------
# Sheet preview for the web UI's row-mapping screen (pure, no I/O).
# ---------------------------------------------------------------------------

_MAX_PREVIEW_ROWS = 150
_MAX_PREVIEW_COLS = 60
_MAX_CELL_CHARS = 80


def inspect_grid(
    tab: str,
    grid: list[list[str]],
    cfg,
    picks: Optional[dict] = None,
) -> dict:
    """Everything the row-mapping screen shows for one tab.

    `picks` lets the UI try unsaved choices; when None, the saved
    tab_overrides are used. Returns the trimmed grid, what auto-detection
    finds on its own, the rows actually in effect, and what each event column
    parses to under those rows (start shown WITHOUT the lead-in).
    """
    label_col = _col_letter_to_index(cfg.sheet.header_column)
    ov = cfg.tab_overrides.get(tab)
    if picks is None:
        picks = ov.rows if ov else {}

    auto = locate_rows(grid, label_col, cfg.sheet.labels, {})
    effective = locate_rows(grid, label_col, cfg.sheet.labels, picks)
    row_of = {fld: m.row for fld, m in effective.items()}

    # Trim trailing blank rows/cols (Football has ~990 mostly-empty rows).
    last_row = max((r for r, row in enumerate(grid) if any(str(v).strip() for v in row)), default=-1)
    n_rows = min(last_row + 1, _MAX_PREVIEW_ROWS)
    n_cols_total = max((len(r) for r in grid[: last_row + 1]), default=0)
    n_cols = min(n_cols_total, _MAX_PREVIEW_COLS)
    rows = [
        {"row": r + 1,
         "cells": [_cell(grid, r, c)[:_MAX_CELL_CHARS] for c in range(n_cols)]}
        for r in range(n_rows)
    ]

    columns = [_describe_column(grid, row_of, col, cfg) for col in range(label_col + 1, n_cols)]
    ready = sum(1 for c in columns if c["status"] == "ok")
    no_room = sum(1 for c in columns if c["status"] == "no_room")
    no_start = sum(1 for c in columns if c["status"] == "no_start")

    missing = []
    if row_of["event_name"] is None:
        missing.append("event name")
    if row_of["date"] is None:
        missing.append("date")
    if row_of["time"] is None:
        missing.append("start time")

    return {
        "tab": tab,
        "label_col": label_col,
        "col_letters": [_col_index_to_letter(c) for c in range(n_cols)],
        "rows": rows,
        "total_rows": last_row + 1,
        "total_cols": n_cols_total,
        "auto": {f: m.to_dict(grid, label_col) for f, m in auto.items()},
        "effective": {f: m.to_dict(grid, label_col) for f, m in effective.items()},
        "columns": columns,
        "summary": {"ready": ready, "no_room": no_room, "no_start": no_start,
                    "missing": missing},
    }


def _describe_column(grid, row_of, col, cfg) -> dict:
    """What one event column yields under the given row mapping."""
    out = {"col": col + 1, "letter": _col_index_to_letter(col), "name": "",
           "start": None, "room": "", "status": "empty", "problem": ""}
    name = _cell(grid, row_of.get("event_name"), col)
    if not name:
        return out
    out["name"] = name

    start = _parse_start(grid, row_of, col, cfg)
    room = _normalize_room(_cell(grid, row_of.get("pcr"), col))
    out["room"] = room or ""

    if start is None:
        raw = " ".join(x for x in (_cell(grid, row_of.get("date"), col),
                                   _cell(grid, row_of.get("time"), col)) if x)
        out["status"] = "no_start"
        out["problem"] = (f"Date has no year ({raw!r}) — add the year in the sheet" if _lacks_year(raw)
                          else f"No usable start ({raw!r})" if raw else "No date/time")
        return out
    out["start"] = start.isoformat()
    if not room:
        out["status"] = "no_room"
        out["problem"] = "No PCR"
        return out
    out["status"] = "ok"
    return out


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
            ev.end = ev.start + timedelta(hours=cfg.scheduling.cap_hours_for(ev.name))
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


def _lacks_year(text: str) -> bool:
    """True if `text` parses as a date/time but has no year in it."""
    try:
        return bool(text) and not _parse_naive_with_year_flag(text)[1]
    except (ValueError, OverflowError):
        return False


def _normalize_room(value: str) -> Optional[str]:
    """Normalize a control-room cell to a canonical letter A..E.

    Accepts 'A', 'PCR A', 'Control Room B'. Returns None for blanks / 'N/A' /
    anything unrecognized. Numeric "CR n" forms are intentionally NOT mapped --
    live tabs use letters, and silently rewriting numbers would hide mistakes.
    """
    if value is None:
        return None
    v = str(value).strip().upper()
    if not v or v in ("N/A", "NA", "TBD", "TBA", "-", "OFF", "NONE"):
        return None

    # Prefer a standalone single-letter token (handles "PCR A", "CONTROL ROOM B").
    for tok in v.replace("-", " ").split():
        if len(tok) == 1 and tok in _ROOM_LETTERS:
            return tok
    # Last resort: a trailing A..E letter.
    letters = [c for c in v if c in _ROOM_LETTERS]
    return letters[-1] if letters else None


def _col_index_to_letter(idx: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA' ..."""
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _col_letter_to_index(letter: str) -> int:
    """'A' -> 0, 'B' -> 1, 'AA' -> 26 ..."""
    idx = 0
    for ch in letter.strip().upper():
        if not ch.isalpha():
            continue
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(idx - 1, 0)
