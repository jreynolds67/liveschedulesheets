"""Shared data structures."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ScheduledEvent:
    """One event parsed from a sheet column, ready to become an LSP event."""

    name: str            # event name from the sheet (e.g. "FB vs UCF")
    pcr: str             # normalized control-room letter (A..E); "" if unassigned
    start: datetime      # timezone-aware; already includes lead-in padding
    end: datetime        # timezone-aware safety-cap end
    source_tab: str      # worksheet tab it came from
    source_column: int   # 1-based column index (for logging)
    event_date: str = ""  # "YYYY-MM-DD" of the sheet date (override matching)
    occurrence: int = 0   # 0-based index among same (name, date) in this tab
    same_day: int = 0     # how many same (name, date) columns the tab has; 0 = unknown
    sheet_start: str = ""  # the sheet's own start, "YYYY-MM-DDTHH:MM" local, before fixes

    def override_key(self) -> tuple[str, str, int]:
        """Stable identity for UI overrides: (date, name, occurrence)."""
        return (self.event_date, self.name.strip().lower(), self.occurrence)

    def is_same_game(self, occurrence: int, same_day: int = 0, sheet_start: str = "") -> bool:
        """Whether something remembered about a game with this name and date
        (an event fix, a tracked record) is about this one.

        By its number among the day's same-named games, unless that count has
        changed since (a doubleheader game added or removed renumbers the
        rest): then only by the sheet's start time, so game 1's fix or LSP
        event never slides onto game 2.
        """
        if same_day and self.same_day and same_day != self.same_day:
            return bool(sheet_start) and sheet_start == self.sheet_start
        return occurrence == self.occurrence

    def lsp_name(self, prefix: str = "") -> str:
        return f"{prefix}{self.name}".strip()
