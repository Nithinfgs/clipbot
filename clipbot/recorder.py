"""streamlink | ffmpeg -c copy -> 6s .ts segments named by wall-clock epoch; rolling buffer."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import time
from collections import deque

from . import log
from .config import BUFFER

L = log.get("recorder")
AD_RE = re.compile(r"advertisement break of (\d+(?:\.\d+)?) seconds", re.I)


def seg_ts(path) -> float:
    return float(path.stem)


class Recorder:
    def __init__(self, name: str, url: str, quality: str, seg_s: int, keep_s: int, on_alert, is_live):
        self.name, self.url, self.quality = name, url, quality
        self.seg_s, self.keep_s = seg_s, keep_s
        self.dir = BUFFER / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.on_alert = on_alert
        self.is_live = is_live               # callable -> bool | None
        self.proc: asyncio.subprocess.Process | None = None
        self.restarts: deque = deque()
        self.ad_ranges: deque = deque(maxlen=50)   # (start, end) wall clock
        self.ad_until = 0.0
        self.healthy = False
        self.started_at = 0.0
        self.stopping = False

    def segments(self):
        return sorted(self.dir.glob("*.ts"), key=seg_ts)

    def newest_age(self) -> float:
        segs = self.segments()
        if not segs:
            return 1e9
        try:
            return time.time() - segs[-1].stat().st_mtime
        except FileNotFoundError:
            return 1e9

    def in_ad(self, a: float, b: float) -> bool:
        return any(s < b and a < e for s, e in self.ad_ranges)

    def prune(self) -> None:
        cutoff = time.time() - self.keep_s
        for f in self.segments():
            try:
                if seg_ts(f) < cutoff:
                    f.unlink()
            except (FileNotFoundError, ValueError):
                pass

    async def _start(self) -> None:
        out = shlex.quote(str(self.dir / "%s.ts"))
        cmd = (
            f"streamlink --stdout --loglevel info --stream-segment-threads 2 "
            f"{shlex.quote(self.url)} {shlex.quote(self.quality)} "
            f"| ffmpeg -hide_banner -loglevel error -fflags +genpts -i pipe:0 -map 0 -c copy "
            f"-f segment -segment_time {self.seg_s} -reset_timestamps 1 -strftime 1 {out}"
        )
        self.proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            env={**os.environ, "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", "")},
        )
        self.started_at = time.time()
        asyncio.create_task(self._read_stderr(self.proc))

    async def _read_stderr(self, proc) -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            m = AD_RE.search(line)
            if m:
                dur = float(m.group(1))
                now = time.time()
                self.ad_until = now + dur + 5
                self.ad_ranges.append((now - 2, now + dur + 5))
                L.info("%s ad break %ss -> unclippable", self.name, dur)
            elif "error" in line.lower() and "No playable streams" not in line:
                L.debug("%s: %s", self.name, line[:200])

    async def _kill(self) -> None:
        p = self.proc
        self.proc = None
        if not p or p.returncode is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            await asyncio.wait_for(p.wait(), 5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        self.stopping = True
        await self._kill()

    async def run(self) -> None:
        backoff = 2
        while not self.stopping:
            if not self.is_live():          # False or None (unknown yet) -> don't spawn
                self.healthy = False
                await self._kill()
                await asyncio.sleep(15)
                continue
            if self.proc is None or self.proc.returncode is not None:
                await self._start()
                await asyncio.sleep(12)
            self.prune()
            age = self.newest_age()
            stale_limit = 30 if time.time() > self.ad_until else 120
            dead = self.proc is None or self.proc.returncode is not None
            if dead or (age > stale_limit and time.time() - self.started_at > stale_limit):
                self.healthy = False
                now = time.time()
                self.restarts.append(now)
                while self.restarts and self.restarts[0] < now - 600:
                    self.restarts.popleft()
                L.warning("%s recorder restart (dead=%s age=%.0fs), #%d in 10min",
                          self.name, dead, min(age, 9999), len(self.restarts))
                if len(self.restarts) >= 5:
                    self.on_alert(f"recorder:{self.name}", f"{self.name}: 5 recorder restarts in 10 min")
                await self._kill()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            if age < 15:
                self.healthy = True
                backoff = 2
            await asyncio.sleep(5)
        await self._kill()
