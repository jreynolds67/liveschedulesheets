"""Read the composite schedule tab(s) from Google Sheets and parse events.

Layout is transposed / form-style: row headers run down `header_column`, and
each subsequent column is a single event.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from dateutil import parser as dateparser
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .config import Config
from .models import ScheduledEvent

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetReader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        creds = Credentials.from_service_account_file(
            cfg.google_credentials_file, scopes=SCOPES
        )
        # cache_discovery=False avoids a noisy warning and a file-cache dependency.
        self.service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    def read_events(self) -> list[ScheduledEvent]:
        events: list[ScheduledEvent] = []
        for tab in self.cfg.sheet.tabs:
            try:
                events.extend(self._read_tab(tab))
            except Exception:  # noqa: BLE001 - one bad tab shouldn't kill the run
                log.exception("Failed to read tab %r", tab)
        return events

    # -- internals ----------------------------------------------------------

    def _read_tab(self, tab: str) -> list[ScheduledEvent]:
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
        grid: list[list[str]] = resp.get("values", [])
        if not grid:
            log.warning("Tab %r is empty", tab)
            return []

        label_col = _col_letter_to_index(self.cfg.sheet.header_column)
        row_of = self._locate_label_rows(grid, label_col)

        required = ["event_name", "pcr"]
        missing = [f for f in required if row_of.get(f) is None]
        if missing:
            log.error("Tab %r missing required label row(s): %s", tab, missing)
            return []
        if row_of.get("datetime") is None and (
            row_of.get("date") is None or row_of.get("time") is None
        ):
            log.error(
                "Tab %r has no start label: need a `datetime` row, or both `date` and `time`",
                tab,
            )
            return []

        n_cols = max(len(r) for r in grid)
        out: list[ScheduledEvent] = []
        for col in range(label_col + 1, n_cols):
            ev = self._parse_column(tab, grid, row_of, col)
            if ev is not None:
                out.append(ev)
        log.info("Tab %r: parsed %d schedulable event(s)", tab, len(out))
        return out

    def _locate_label_rows(
        self, grid: list[list[str]], label_col: int
    ) -> dict[str, Optional[int]]:
        """Map each field to the first grid row whose header matches an alias."""
        labels = self.cfg.sheet.labels
        wanted = {
            "event_name": labels.event_name,
            "pcr": labels.pcr,
            "date": labels.date,
            "time": labels.time,
            "datetime": labels.datetime,
        }
        norm_wanted = {
            field: [a.strip().lower() for a in aliases]
            for field, aliases in wanted.items()
        }
        found: dict[str, Optional[int]] = {f: None for f in wanted}
        for r, row in enumerate(grid):
            if label_col >= len(row):
                continue
            header = row[label_col].strip().lower()
            if not header:
                continue
            for field, aliases in norm_wanted.items():
                if found[field] is None and header in aliases:
                    found[field] = r
        return found

    def _cell(self, grid, row: Optional[int], col: int) -> str:
        if row is None or row >= len(grid) or col >= len(grid[row]):
            return ""
        return grid[row][col].strip()

    def _parse_column(
        self, tab: str, grid, row_of: dict[str, Optional[int]], col: int
    ) -> Optional[ScheduledEvent]:
        name = self._cell(grid, row_of.get("event_name"), col)
        pcr_raw = self._cell(grid, row_of.get("pcr"), col)
        if not name or not pcr_raw:
            return None  # not a schedulable column yet

        pcr = _normalize_pcr(pcr_raw)
        if not pcr:
            log.debug("Col %d (%s): PCR value %r not a letter; skipping", col + 1, name, pcr_raw)
            return None
        if pcr not in self.cfg.pcr_channel_map:
            log.warning(
                "Col %d (%s): PCR %r has no channel mapping; skipping", col + 1, name, pcr
            )
            return None

        start = self._parse_start(grid, row_of, col)
        if start is None:
            log.debug("Col %d (%s): no usable start time; skipping", col + 1, name)
            return None

        start = start - timedelta(minutes=self.cfg.scheduling.lead_in_minutes)
        end = start + timedelta(hours=self.cfg.scheduling.safety_cap_hours)
        return ScheduledEvent(
            name=name,
            pcr=pcr,
            start=start,
            end=end,
            source_tab=tab,
            source_column=col + 1,
        )

    def _parse_start(self, grid, row_of, col) -> Optional[datetime]:
        skip = self.cfg.date_parsing.skip_values
        tz = self.cfg.sheet.timezone

        # Preferred: a single explicit datetime cell.
        dt_val = self._cell(grid, row_of.get("datetime"), col)
        if dt_val:
            if dt_val.strip().upper() in skip:
                return None
            return self._parse_datetime_string(dt_val, tz)

        # Fallback: separate date + time cells.
        date_val = self._cell(grid, row_of.get("date"), col)
        time_val = self._cell(grid, row_of.get("time"), col)
        if not date_val or not time_val:
            return None
        if date_val.strip().upper() in skip or time_val.strip().upper() in skip:
            return None
        return self._parse_datetime_string(f"{date_val} {time_val}", tz)

    def _parse_datetime_string(self, text: str, tz) -> Optional[datetime]:
        try:
            dt, had_year = _parse_naive_with_year_flag(text)
        except (ValueError, OverflowError):
            log.warning("Could not parse date/time %r", text)
            return None
        if not had_year:
            dt = dt.replace(year=self._infer_year(dt.month))
        return dt.replace(tzinfo=tz)

    def _infer_year(self, month: int) -> int:
        dp = self.cfg.date_parsing
        if month >= dp.rollover_month:
            return dp.academic_year_start
        return dp.academic_year_start + 1


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


def _normalize_pcr(value: str) -> Optional[str]:
    """Extract the PCR letter from values like 'A', 'PCR A', 'Control Room B'."""
    letters = [c for c in value.upper() if c.isalpha()]
    if not letters:
        return None
    # Prefer a standalone single-letter token, else the last alpha char.
    tokens = value.upper().replace("-", " ").split()
    for tok in tokens:
        if len(tok) == 1 and tok.isalpha():
            return tok
    return letters[-1]


def _col_letter_to_index(letter: str) -> int:
    """'A' -> 0, 'B' -> 1, 'AA' -> 26 ..."""
    idx = 0
    for ch in letter.strip().upper():
        if not ch.isalpha():
            continue
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(idx - 1, 0)
