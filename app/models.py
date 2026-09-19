"""Shared data structures."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ScheduledEvent:
    """One event parsed from a sheet column, ready to become an LSP event."""

    name: str            # event name from the sheet (e.g. "FB vs UCF")
    pcr: str             # normalized PCR letter (A/B/C/D/E/V)
    start: datetime      # timezone-aware; already includes lead-in padding
    end: datetime        # timezone-aware safety-cap end
    source_tab: str      # worksheet tab it came from
    source_column: int   # 1-based column index (for logging)

    def dedup_key(self) -> str:
        """Stable identity used for local-state de-duplication."""
        raw = f"{self.pcr}|{self.name.strip().lower()}|{self.start.astimezone().isoformat()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def lsp_name(self, prefix: str = "") -> str:
        return f"{prefix}{self.name}".strip()
