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
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

from dateutil import parser as dateparser

from .config import ROW_FIELDS, Config
from .models import ScheduledEvent, name_key

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Canonical control rooms (PCRs). The PCR / CONTROL ROOM row holds a letter,
# bare ("A") or prefixed ("PCR A").
_ROOM_LETTERS = ["A", "B", "C", "D", "E"]

# Retries (with backoff) on Google 5xx / rate-limit replies.
_GOOGLE_RETRIES = 3


class SheetError(Exception):
    """The sheet couldn't be read (Google API / sharing / key problem)."""


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

    def read_events(self, known_tabs: Optional[set] = None) -> list[ScheduledEvent]:
        """Every schedulable event on the enabled tabs.

        Raises SheetError if the sheet can't be opened at all (so a revoked
        share or key fails the pass loudly instead of looking like an empty
        sheet). A single tab that fails is skipped and named in
        `self.errors`; its tracked events then show as "not in sheet", which
        never deletes anything.

        `known_tabs` are the tabs the tool has read before (None: not
        recorded yet). A visible tab not among them, and not turned on in the
        UI, is new (an added or renamed tab, "Copy of Basketball"...): it
        isn't read until someone turns it on. `self.warnings` names those,
        tab settings that match no tab any more, tabs whose rows can't be
        found, and columns with a name but no usable date or time.
        `self.read_tabs` lists the tabs read.
        """
        self.errors: list[str] = []
        self.warnings: list[dict] = []
        self.read_tabs: list[str] = []
        events: list[ScheduledEvent] = []
        for tab in self._tabs_to_read():
            if not self._tab_enabled(tab, known_tabs):
                continue
            self.read_tabs.append(tab)
            try:
                grid = self.fetch_grid(tab)
                events.extend(parse_grid(tab, grid, self.cfg, problems=self.warnings))
            except Exception as exc:  # noqa: BLE001 - one bad tab shouldn't kill the run
                log.exception("Failed to read tab %r", tab)
                self.errors.append(f"Could not read tab {tab!r}: {exc}")
        return events

    def _tabs_to_read(self) -> list[str]:
        """Which tabs to parse: the visible tabs, minus hidden ones.

        Hidden tabs (COUNT, the stale *RELAYOUT composites) are skipped
        automatically. If `sheet.tabs` is configured, it is used as an explicit
        allow-list but hidden tabs are still dropped; if it is empty, every
        visible tab is read (auto-discovery).
        """
        try:
            tabs = self.sheet_meta()["tabs"]
        except Exception as exc:  # noqa: BLE001 - any Google failure
            raise SheetError(f"Could not open the Google Sheet: {exc}") from exc
        visible = self.visible = [t["name"] for t in tabs if not t["hidden"]]
        names = {t["name"] for t in tabs}
        for tab in self.cfg.tab_overrides:
            if tab not in names:
                self.warnings.append(_warning(
                    tab, f"Settings for tab “{tab}” match no tab in the sheet (renamed?): "
                         "set the renamed tab up again under Sheet tabs & row mapping"))

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
            .execute(num_retries=_GOOGLE_RETRIES)
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
                range=a1_tab(tab),
                valueRenderOption="FORMATTED_VALUE",
                dateTimeRenderOption="FORMATTED_STRING",
            )
            .execute(num_retries=_GOOGLE_RETRIES)
        )
        # Google returns a merged cell's value in its first cell only; a DATE
        # merged across a doubleheader's two columns would leave game 2
        # without one.
        meta = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.cfg.sheet.spreadsheet_id, ranges=[a1_tab(tab)],
                 fields="sheets(merges)")
            .execute(num_retries=_GOOGLE_RETRIES)
        )
        merges = ((meta.get("sheets") or [{}])[0] or {}).get("merges") or []
        return fill_merges(resp.get("values", []), merges,
                           _col_letter_to_index(self.cfg.sheet.header_column))

    def _tab_enabled(self, tab: str, known_tabs: Optional[set] = None) -> bool:
        override = self.cfg.tab_overrides.get(tab)
        if override is not None:
            if not override.enabled:
                log.info("Tab %r is turned off; skipping", tab)
            return override.enabled
        if known_tabs is not None and tab not in known_tabs and not self.cfg.sheet.tabs:
            log.info("New tab %r is not read until it is turned on in the UI", tab)
            self.warnings.append(_warning(
                tab, f"New tab “{tab}” isn't read until you turn on Include this tab "
                     "(Sheet tabs & row mapping) and Save"))
            return False
        return True


# ---------------------------------------------------------------------------
# Pure parsing (no I/O) -- tested directly against a real workbook.
# ---------------------------------------------------------------------------

def parse_grid(tab: str, grid: list[list[str]], cfg: Config,
               problems: Optional[list] = None) -> list[ScheduledEvent]:
    """Turn one tab's cell grid into schedulable events.

    `grid` is row-major (grid[row][col]); cells are strings (empty for blanks).
    `problems`, if given, gets a warning (see _warning) for a tab whose rows
    can't be found and for each column that has an event name and a date and
    time typed in but can't be scheduled (no AM/PM, no year...), so a typo
    is reported instead of the game silently not being recorded. Blank or
    TBD dates and times, games more than a day past, and columns ignored by
    an event fix aren't reported.
    """
    problems = problems if problems is not None else []
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
            log.debug("Tab %r: %s -> row %s (%s)", tab, fld,
                      "none" if m.row is None else m.row + 1, m.note or m.method)

    missing = [what for fld, what in (("event_name", "EVENT"), ("date", "DATE"),
                                      ("time", "start time")) if row_of[fld] is None]
    if missing:
        log.info("Tab %r: no %s row found; skipping", tab, " / ".join(missing))
        problems.append(_warning(
            tab, f"Tab “{tab}” is not read: no {' / '.join(missing)} row found. Pick the rows "
                 "under Sheet tabs & row mapping, or turn the tab off if it holds no games"))
        return []

    if row_of["pcr"] is None:
        log.debug("Tab %r: no PCR row; events will need a PCR assigned", tab)

    n_cols = max(len(r) for r in grid)
    out: list[ScheduledEvent] = []
    seen: dict[tuple, int] = {}  # (name, date) -> count, for override disambiguation
    for col in range(label_col + 1, n_cols):
        ev = _parse_column(tab, grid, row_of, col, cfg, seen)
        if ev is not None:
            out.append(ev)
        else:
            why = _unschedulable(tab, grid, row_of, col, cfg, seen)
            if why:
                # Known by its cells, not its column letter, so inserting a
                # column doesn't make every warning on the tab look new.
                key = "|".join([tab] + [_cell(grid, row_of.get(f), col)
                                        for f in ("event_name", "date", "time")])
                problems.append(_warning(tab, why, col + 1, key))
    for ev in out:
        ev.same_day = seen.get((name_key(ev.name), ev.event_date), 1)

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
_ROOM_JOINED_RE = re.compile(r"(?:PCR|CONTROLROOM|CR)?([A-E])")
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
        aliases = {clean_text(a).lower() for a in getattr(labels, fld)}
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
            vals = [clean_text(v) for c, v in enumerate(row) if c > label_col and clean_text(v)]
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
    want = clean_text(pick.label).lower()
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
    return clean_text(grid[row][col])


# Characters that show as nothing in Sheets but break matching (zero-width
# spaces, joiners, a byte-order mark, soft hyphens), often pasted in with text.
_INVISIBLE_RE = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff]")


def clean_text(value) -> str:
    """A cell as text: invisible characters dropped, and runs of spaces,
    non-breaking spaces and line breaks (Alt+Enter) made one space."""
    return " ".join(_INVISIBLE_RE.sub("", str(value)).split())


def _parse_column(tab, grid, row_of, col, cfg, seen) -> Optional[ScheduledEvent]:
    name = _cell(grid, row_of.get("event_name"), col)
    if not name:
        return None  # empty column, not an event

    # Number same-named games on a day (doubleheaders) BEFORE looking at the
    # start time, so game 2 keeps its number (and its event fixes / tracked
    # record) whether or not game 1's time is filled in yet.
    date_str = _date_key(_cell(grid, row_of.get("date"), col), cfg)
    occurrence = 0
    if date_str:
        bucket = (name_key(name), date_str)
        occurrence = seen.get(bucket, 0)
        seen[bucket] = occurrence + 1

    start = _parse_start(grid, row_of, col, cfg)
    if start is None:
        log.debug("Tab %r col %d (%s): no usable start time; skipping", tab, col + 1, name)
        return None
    date_str = date_str or _event_date_key(start)

    pcr = _normalize_room(_cell(grid, row_of.get("pcr"), col))  # None -> unassigned (fix in UI)

    sheet_start = start.strftime("%Y-%m-%dT%H:%M")
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
        sheet_start=sheet_start,
        note=_called_off_note(grid, row_of, col),
    )


# Words that say a game may be off. Such a column is still scheduled (a
# makeup game's cell often says "PPD from 9/10", and a missed recording can't
# be redone) but Preview says so, so someone checks it.
_CALLED_OFF_RE = re.compile(r"\b(cancell?ed|cancel|postponed?|ppd|suspended|rain(?:ed)? out)\b",
                            re.IGNORECASE)


def _called_off_note(grid, row_of, col) -> str:
    for fld in ("event_name", "date", "time"):
        m = _CALLED_OFF_RE.search(_cell(grid, row_of.get(fld), col))
        if m:
            return (f"the sheet says “{m.group(0)}”: check it is still on "
                    "(if not, cancel or delete it in LSP)")
    return ""


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
    return _parse_datetime_string(f"{date_val} {time_val}", tz)


def _warning(tab: str, text: str, column: Optional[int] = None,
             key: Optional[str] = None) -> dict:
    """A sheet problem for Status and Preview. `key` identifies it, so an
    alert about it is sent once, not every pass (by default its text)."""
    return {"tab": tab, "column": column, "text": text, "key": key or f"{tab}: {text}"}


def _unschedulable(tab, grid, row_of, col, cfg, seen) -> Optional[str]:
    """Why a column with an event name, and a date and time typed in, can't
    be scheduled; None if it isn't worth reporting (blank or TBD, more than a
    day past, or ignored by an event fix)."""
    name = _cell(grid, row_of.get("event_name"), col)
    date_raw = _cell(grid, row_of.get("date"), col)
    time_raw = _cell(grid, row_of.get("time"), col)
    skip = cfg.date_parsing.skip_values
    if not (name and date_raw and time_raw) or date_raw.upper() in skip or time_raw.upper() in skip:
        return None
    date_str = _date_key(date_raw, cfg)
    if date_str:
        today = datetime.now(cfg.sheet.timezone).date()
        if datetime.strptime(date_str, "%Y-%m-%d").date() < today - timedelta(days=1):
            return None
        occurrence = seen.get((name_key(name), date_str), 1) - 1
        if any(o.ignore and o.date == date_str and o.name == name_key(name)
               and o.occurrence == occurrence for o in cfg.event_overrides.get(tab, [])):
            return None
    where = f"{tab} column {_col_index_to_letter(col)}"
    if _lacks_year(date_raw):
        return f"“{name}” ({where}) isn't scheduled: the date “{date_raw}” has no year"
    if _lacks_ampm(time_raw):
        return f"“{name}” ({where}) isn't scheduled: the time “{time_raw}” has no AM/PM"
    return f"“{name}” ({where}) isn't scheduled: can't read “{date_raw} {time_raw}” as a date and time"


def _date_key(date_val: str, cfg) -> Optional[str]:
    """"YYYY-MM-DD" for a date cell on its own, or None if it has no usable
    date (blank, TBD, no year)."""
    if not date_val or date_val.upper() in cfg.date_parsing.skip_values:
        return None
    try:
        dt, had_year = _parse_naive_with_year_flag(date_val)
    except (ValueError, OverflowError):
        return None
    return _event_date_key(dt) if had_year else None


def _parse_datetime_string(text: str, tz) -> Optional[datetime]:
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
    last_row = max((r for r, row in enumerate(grid) if any(clean_text(v) for v in row)), default=-1)
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
        time_raw = _cell(grid, row_of.get("time"), col)
        out["status"] = "no_start"
        out["problem"] = (f"Date has no year ({raw!r}) — add the year in the sheet" if _lacks_year(raw)
                          else f"Start time has no AM/PM ({time_raw!r}) — add it in the sheet"
                          if _lacks_ampm(time_raw)
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
        ov = next((o for o in overrides
                   if o.date == ev.event_date and o.name == name_key(ev.name)
                   and ev.is_same_game(o.occurrence, o.same_day, o.sheet_start)), None)
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

def fill_merges(grid: list[list[str]], merges: list[dict], label_col: int = 0) -> list[list[str]]:
    """Copy a merged cell's value (Google returns it in the first cell only)
    across the event columns it spans, as it shows in the sheet: a DATE or
    PCR merged over a doubleheader's two columns applies to both games.

    Only along its first row: a cell merged down into the rows below would
    otherwise fill another field's row (a name landing in GAME START). Merges
    starting in the label column (section banners) are left alone. `merges`
    are Sheets API GridRanges (0-based, end exclusive)."""
    n_cols = max((len(r) for r in grid), default=0)
    for m in merges or []:
        r, c0 = m.get("startRowIndex", 0), m.get("startColumnIndex", 0)
        c1 = min(m.get("endColumnIndex", n_cols), n_cols)
        if c0 <= label_col or r >= len(grid) or c0 >= len(grid[r]) or not clean_text(grid[r][c0]):
            continue
        row = grid[r]
        row.extend([""] * (c1 - len(row)))
        for c in range(c0 + 1, c1):
            if not clean_text(row[c]):
                row[c] = row[c0]
    return grid


def a1_tab(tab: str) -> str:
    """A whole-tab A1 range: the name quoted, with any ' doubled, so tabs
    like "Women's Basketball" parse."""
    return "'" + tab.replace("'", "''") + "'"


def _event_date_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?(:\d{2})?\s*([AaPp]\.?[Mm]\.?)?(?![\w:])")
_AMPM_RE = re.compile(r"\b([AaPp])\.?[Mm]\b\.?")
_NOON_RE = re.compile(r"\bnoon\b", re.IGNORECASE)
# "Noon" / "12 Noon" / "12:00 noon", written out as "12:00 NOON"
_NOON_TIME_RE = re.compile(r"(?:\b12(?::00)?\s*)?\bnoon\b", re.IGNORECASE)


def _first_time_token(text: str) -> Optional[str]:
    """Pull the first usable clock time out of a messy cell.

    "7 PM" -> "7:00 PM"; "6:30 & 9:00 PM" -> "6:30 PM" (the AM/PM later in the
    cell applies); "17:30" and "9:00:00" -> 24-hour; "12 NOON" / "Noon" -> "12:00 PM".
    Returns None when there's no time, for a bare number ("@2"), and for an
    hour 1-12 with no AM/PM anywhere in the cell ("7:00 ET"): that could be
    morning or evening, and guessing morning is how a 7 PM game gets recorded
    at 7 AM.
    """
    t = _NOON_TIME_RE.sub("12:00 NOON", text.strip())
    for m in _TIME_RE.finditer(t):
        hour, minute, seconds, ampm = m.group(1), m.group(2), m.group(3), m.group(4)
        if minute is None and ampm is None:
            continue  # bare number (e.g. "@2") is too ambiguous to schedule on
        h = int(hour)
        if ampm is None:
            later = _AMPM_RE.search(t, m.end())
            if later:
                ampm = later.group(1) + "M"
            elif _NOON_RE.search(t) and h == 12:
                ampm = "PM"
            elif not (seconds or h == 0 or h >= 13):
                return None  # 1-12 with no AM/PM: morning or evening?
        out = f"{h}:{minute or '00'}"
        if ampm:
            out += f" {ampm[0].upper()}M"
        return out
    return None


def _lacks_ampm(text: str) -> bool:
    """True if a time cell holds a clock time we refuse for want of AM/PM."""
    return bool(text) and _first_time_token(text) is None and bool(
        re.search(r"\b\d{1,2}:\d{2}\b", text))


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
    # Run together ("PCRA"). Nothing looser: picking any A..E letter out of
    # the cell turned "CR3" into C and "HOME" into E.
    m = _ROOM_JOINED_RE.fullmatch(re.sub(r"[\s-]", "", v))
    return m.group(1) if m else None


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
