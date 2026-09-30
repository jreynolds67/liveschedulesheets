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

    def override_key(self) -> tuple[str, str, int]:
        """Stable identity for UI overrides: (date, name, occurrence)."""
        return (self.event_date, self.name.strip().lower(), self.occurrence)

    def lsp_name(self, prefix: str = "") -> str:
        return f"{prefix}{self.name}".strip()
