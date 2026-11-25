"""Bandwidth accounting + a token-bucket limiter for VOD fetches.

live_mbps  = bytes written to the rolling buffer (measured from segment files)
vod_mbps   = bytes pulled through the limiter
iface_mbps = whole-machine download rate from `netstat -ib` (ground truth)
VOD allowance = cap - live_mbps, never below 3 Mbps.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
from collections import deque

from .config import BUFFER


class Bandwidth:
    def __init__(self, cap_mbps: float):
        self.cap = cap_mbps
        self.vod_events: deque = deque()     # (ts, bytes)
        self.live_mbps = 0.0
        self.vod_mbps = 0.0
        self.iface_mbps = 0.0
        self.iface_up_mbps = 0.0
        self._tokens = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._iface_prev: tuple[float, int, int] | None = None
        self.history: deque = deque(maxlen=720)   # (ts, iface, live, vod) every 5s = 1h

    @property
    def vod_allowance_bps(self) -> float:
        return max(self.cap - self.live_mbps, 3.0) * 1e6 / 8

    def headroom_mbps(self) -> float:
        return self.cap - max(self.iface_mbps, self.live_mbps + self.vod_mbps)

    async def acquire(self, nbytes: int) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                rate = self.vod_allowance_bps
                self._tokens = min(self._tokens + (now - self._last) * rate, rate * 0.5)  # 0.5s burst
                self._last = now
                if self._tokens >= nbytes or nbytes > rate * 0.5 and self._tokens >= rate * 0.5:
                    self._tokens -= nbytes
                    break
                await asyncio.sleep(max((nbytes - self._tokens) / rate, 0.01))
        self.vod_events.append((time.time(), nbytes))

    def _sample_live(self, window: float = 30.0) -> float:
        now = time.time()
        total = 0
        for d in BUFFER.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.ts"):
                try:
                    st = f.stat()
                except FileNotFoundError:
                    continue
                if now - st.st_mtime <= window:
                    total += st.st_size
        return total * 8 / window / 1e6

    def _sample_iface(self) -> None:
        try:
            out = subprocess.run(["netstat", "-ibn"], capture_output=True, text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001
            return
        rx = tx = 0
        seen = set()
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 10 and re.match(r"^(en|bridge)\d+$", p[0]) and p[2].startswith("<Link") and p[0] not in seen:
                seen.add(p[0])
                try:
                    rx += int(p[6])
                    tx += int(p[9])
                except ValueError:
                    pass
        now = time.time()
        if self._iface_prev:
            t0, rx0, tx0 = self._iface_prev
            dt = now - t0
            if dt > 0 and rx >= rx0:
                self.iface_mbps = (rx - rx0) * 8 / dt / 1e6
                self.iface_up_mbps = (tx - tx0) * 8 / dt / 1e6
        self._iface_prev = (now, rx, tx)

    async def run(self) -> None:
        while True:
            now = time.time()
            while self.vod_events and self.vod_events[0][0] < now - 30:
                self.vod_events.popleft()
            self.vod_mbps = sum(b for _, b in self.vod_events) * 8 / 30 / 1e6
            self.live_mbps = await asyncio.to_thread(self._sample_live)
            await asyncio.to_thread(self._sample_iface)
            self.history.append((now, round(self.iface_mbps, 1), round(self.live_mbps, 1), round(self.vod_mbps, 1)))
            await asyncio.sleep(5)
