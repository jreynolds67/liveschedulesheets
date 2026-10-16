"""Tell someone when syncing stops working, or the sheet needs attention.

Posts a short message to a webhook (`runtime.alert_webhook_url`: Slack,
Microsoft Teams / Power Automate, Discord, or anything taking a JSON POST;
the body carries the message as both `text` and `content`):

- when passes keep failing (two in a row, so one LSP blip isn't an alert),
  again every few hours while it lasts, and once when it recovers;
- once for each new sheet warning (a game that can't be scheduled, a new
  tab waiting to be turned on...); one that goes away and comes back is
  sent again.

What was sent is kept in the status file, so a restart doesn't repeat it.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Failed passes in a row before it's worth waking someone.
FAILS_BEFORE_ALERT = 2
# How often to say it again while syncing stays broken.
REMIND_SECONDS = 6 * 3600
# Most warnings listed in one message.
_MAX_LISTED = 10


class Alerter:
    def __init__(self, saved: Optional[dict] = None):
        saved = saved or {}
        self.fails = int(saved.get("fails") or 0)
        self.alerted = bool(saved.get("alerted"))
        self.last_sent = float(saved.get("last_sent") or 0)
        self.warned: set[str] = set(saved.get("warned") or [])

    def to_dict(self) -> dict:
        return {"fails": self.fails, "alerted": self.alerted, "last_sent": self.last_sent,
                "warned": sorted(self.warned)}

    def pass_done(self, url: str, ok: bool, why: str, warnings: Optional[list[dict]]) -> None:
        """After a pass: `ok` = it ran without errors (`why` says what went
        wrong if not); `warnings` are its sheet warnings ({"text", "key"}),
        or None if the pass broke off before reading the sheet."""
        now = time.time()
        if ok:
            if self.alerted:
                self._send(url, "✅ LiveScheduleSheets: syncing works again.")
            self.fails, self.alerted = 0, False
        else:
            self.fails += 1
            due = (not self.alerted and self.fails >= FAILS_BEFORE_ALERT) or (
                self.alerted and now - self.last_sent >= REMIND_SECONDS)
            if due:
                again = " (still)" if self.alerted else ""
                if self._send(url, f"⚠️ LiveScheduleSheets: syncing is failing{again} "
                                   f"({self.fails} passes in a row): {why}"):
                    self.alerted, self.last_sent = True, now
        if warnings is None:
            return
        keys = {w["key"]: w["text"] for w in warnings}
        new = [text for key, text in keys.items() if key not in self.warned]
        if new and self._send(url, "LiveScheduleSheets needs attention:\n" + "\n".join(
                f"• {t}" for t in new[:_MAX_LISTED])
                + (f"\n…and {len(new) - _MAX_LISTED} more (see Status)" if len(new) > _MAX_LISTED
                   else "")):
            self.warned |= set(keys)
        self.warned &= set(keys)  # gone now: say so again if it comes back

    def send(self, url: str, text: str) -> bool:
        """Send one message now (a test, or a restart notice)."""
        return self._send(url, text)

    def _send(self, url: str, text: str) -> bool:
        if not url:
            return False
        try:
            resp = requests.post(url, json={"text": text, "content": text}, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Could not send alert to the webhook: %s", exc)
            return False
        log.info("Alert sent: %s", text.splitlines()[0])
        return True
