"""Shared data structures."""
from __future__ import annotations

import hashlib
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

    def dedup_key(self, channel_id: str = "") -> str:
        """Stable identity used for local-state de-duplication (per channel)."""
        raw = f"{self.pcr}|{self.name.strip().lower()}|{self.start.astimezone().isoformat()}"
        if channel_id:
            raw += f"|{channel_id}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def lsp_name(self, prefix: str = "") -> str:
        return f"{prefix}{self.name}".strip()
