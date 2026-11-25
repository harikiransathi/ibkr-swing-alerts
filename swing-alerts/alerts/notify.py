"""Telegram alerts, sent from a background thread so a slow or failing Telegram API never blocks the scanner.

Credentials come from the environment (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) or from config/secrets.yaml
(telegram: {bot_token: ..., chat_id: ...}). Without them every send is a no-op that only logs.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

log = logging.getLogger("alerts.notify")
ROOT = Path(__file__).resolve().parents[1]


def _creds() -> tuple[str, str]:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    p = ROOT / "config" / "secrets.yaml"
    if (not tok or not chat) and p.exists():
        t = (yaml.safe_load(p.read_text()) or {}).get("telegram") or {}
        tok, chat = tok or str(t.get("bot_token") or ""), chat or str(t.get("chat_id") or "")
    return tok, chat


class Telegram:
    def __init__(self, prefix: str = "") -> None:
        self.token, self.chat = _creds()
        self.prefix = prefix
        self.q: queue.Queue[str] = queue.Queue(maxsize=500)
        self.enabled = bool(self.token and self.chat)
        if self.enabled:
            threading.Thread(target=self._worker, name="telegram", daemon=True).start()
        else:
            log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID or config/secrets.yaml)")

    def send(self, text: str) -> None:
        log.info("notify: %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            self.q.put_nowait(f"{self.prefix}{text}")
        except queue.Full:
            pass

    def flush(self, timeout: float = 10) -> None:
        """Give queued messages a chance to go out before the process exits."""
        end = time.time() + timeout
        while self.enabled and not self.q.empty() and time.time() < end:
            time.sleep(0.2)

    def _worker(self) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            text = self.q.get()
            for attempt in range(3):
                try:
                    data = urllib.parse.urlencode({"chat_id": self.chat, "text": text[:4000]}).encode()
                    with urllib.request.urlopen(url, data=data, timeout=10) as r:
                        json.loads(r.read())
                    break
                except Exception as ex:  # noqa: BLE001
                    retry = 5.0
                    body = getattr(ex, "read", None)
                    if body:
                        try:
                            retry = float(json.loads(body()).get("parameters", {}).get("retry_after", 5))
                        except Exception:  # noqa: BLE001
                            pass
                    log.warning("Telegram send failed (attempt %d): %s", attempt + 1, ex)
                    time.sleep(min(retry, 30))
            time.sleep(1.0)          # stay far below Telegram's per-chat limit
