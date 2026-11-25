"""Phone alerts via ntfy.sh. Topic comes from NTFY_TOPIC in .env. Deduped: one per key per 15 min."""
from __future__ import annotations

import os
import time

import httpx

from . import log

L = log.get("alerts")


class Alerts:
    def __init__(self, dedupe_s: int = 900):
        self.topic = os.environ.get("NTFY_TOPIC", "").strip()
        self.dedupe_s = dedupe_s
        self.sent: dict[str, float] = {}

    def send(self, key: str, msg: str, priority: str = "high", title: str = "ClipBot") -> bool:
        now = time.time()
        if now - self.sent.get(key, 0) < self.dedupe_s:
            return False
        self.sent[key] = now
        L.error("ALERT [%s] %s", key, msg)
        if not self.topic:
            return False
        try:
            httpx.post(f"https://ntfy.sh/{self.topic}", content=msg.encode(),
                       headers={"Title": title, "Priority": priority, "Tags": "rotating_light"}, timeout=10)
            return True
        except Exception as e:  # noqa: BLE001
            L.warning("ntfy send failed: %s", e)
            return False
