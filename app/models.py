"""Shared data structures."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# Sport codes that start an event name ("MSOC vs UNC"); more can be added in
# the settings (scheduling.sport_codes).
DEFAULT_SPORT_CODES = (
    "FB", "MBB", "WBB", "MSOC", "WSOC", "VB", "WVB", "MVB", "GYM", "WRS", "BSB", "SB",
    "MTEN", "WTEN", "SWIM", "MSWIM", "WSWIM", "DIVE", "TF", "MTF", "WTF", "XC", "MXC", "WXC",
    "LAX", "MLAX", "WLAX", "FH", "GOLF", "MGOLF", "WGOLF", "HOCKEY",
)

_VS_RE = re.compile(r"\s+(?:vs\.?|v\.)\s+", re.IGNORECASE)
# A note in brackets: "(7 PM)", "(EXH)". Not two capitals alone, which are
# part of a team's name: "Miami (OH)".
_NOTE_RE = re.compile(r"\s*\((?![A-Z]{2}\))[^)]*\)")


def format_event_name(name: str, home_team: str, sport_codes=DEFAULT_SPORT_CODES) -> str:
    """The name a sheet event gets in LSP (event and Event Name variable).

    The sheet lists home games by the opponent alone, so with a home team
    set, "MSOC vs UNC" becomes "MSOC UNC vs. Pitt". A name that already has
    two teams keeps them: "VB Jacksonville vs High Point" becomes "VB
    Jacksonville vs. High Point" (as does "UNC vs Duke", which has no sport
    code in front). Notes in brackets ("(7 PM)", "(EXH)") are dropped. A name
    with no "vs", or no home team set, is left as the sheet has it."""
    if not home_team.strip():
        return name
    parts = _VS_RE.split(" ".join(_NOTE_RE.sub("", name).split()), maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return name
    left, right = parts[0].strip(), parts[1].strip()
    words = left.split()
    if not words:  # "vs UNC"
        return f"{right} vs. {home_team.strip()}"
    if words[-1].upper() in {c.strip().upper() for c in sport_codes}:
        # Only the sport before "vs" (anything ahead of it, like a date,
        # stays): a home game.
        return f"{left} {right} vs. {home_team.strip()}"
    return f"{left} vs. {right}"


def name_key(name: str) -> str:
    """An event name for matching: ignoring case and spacing ("VB  vs Pitt"
    and "vb vs pitt" are the same game)."""
    return " ".join((name or "").split()).lower()


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
    note: str = ""         # a warning for Preview, e.g. the sheet says "PPD"

    def override_key(self) -> tuple[str, str, int]:
        """Stable identity for UI overrides: (date, name, occurrence)."""
        return (self.event_date, name_key(self.name), self.occurrence)

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

    def lsp_name(self, prefix: str = "", home_team: str = "",
                 sport_codes=DEFAULT_SPORT_CODES) -> str:
        """The event's name in LSP: see format_event_name."""
        return f"{prefix}{format_event_name(self.name, home_team, sport_codes)}".strip()
