"""Cut a wall-clock window out of a Twitch in-progress VOD (HLS EVENT playlist).

Each VOD segment carries #EXT-X-PROGRAM-DATE-TIME, so [start_ts, end_ts] maps directly
to segment files. Measured: VOD trails live by ~5-15s; a 30s 720p window downloads in ~7s.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yt_dlp

from . import log

L = log.get("vod")
SEG_RE = re.compile(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)\s*\n#EXTINF:([\d.]+),?[^\n]*\n(\S+)")


class VodUnavailable(Exception):
    pass


@dataclass
class VodInfo:
    vod_id: str
    m3u8: str
    resolved_at: float


def _ydl():
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                             "format": "best[height<=720]/best", "socket_timeout": 20})


def _resolve_sync(channel: str) -> VodInfo:
    with _ydl() as y:
        pl = y.extract_info(f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time",
                            download=False, process=False)
        first = next(iter(pl.get("entries") or []), None)
        if not first:
            raise VodUnavailable("no archive VODs (VODs disabled?)")
        vid = str(first.get("id") or first["url"].rstrip("/").split("/")[-1])
        info = y.extract_info(f"https://www.twitch.tv/videos/{vid.lstrip('v')}", download=False)
        url = info.get("url")
        if not url:
            raise VodUnavailable(f"VOD {vid} has no playable URL (sub-only?)")
        return VodInfo(vid, url, time.time())


TDTG_RE = re.compile(r"#ID3-EQUIV-TDTG:(\S+)")
TOTAL_RE = re.compile(r"#EXT-X-TWITCH-TOTAL-SECS:([\d.]+)")
PLAIN_RE = re.compile(r"#EXTINF:([\d.]+),?[^\n]*\n(\S+)")


def _utc(s: str) -> float:
    s = s.replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


MAP_RE = re.compile(r'#EXT-X-MAP:.*?URI="([^"]+)"')


def parse_playlist(text: str) -> list[tuple[float, float, str, str | None]]:
    """[(wall_start, duration, uri, init_map_uri)].
    Wall clock from per-segment PROGRAM-DATE-TIME when present; otherwise VOD start =
    TDTG (playlist write time, UTC) - TWITCH-TOTAL-SECS (verified <1s vs PDT).
    init_map_uri is the fMP4 init segment (#EXT-X-MAP) in effect for that segment, or None for MPEG-TS."""
    raw: list[list] = []          # [pdt|None, dur, uri, map]
    cur_map: str | None = None
    pdt: float | None = None
    dur: float | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP"):
            m = MAP_RE.search(line)
            cur_map = m.group(1) if m else None
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = _utc(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif not line.startswith("#") and dur is not None:
            raw.append([pdt, dur, line, cur_map])
            pdt, dur = None, None
    if not raw:
        return []
    if all(r[0] is not None for r in raw):
        return [tuple(r) for r in raw]
    m_t, m_tot = TDTG_RE.search(text), TOTAL_RE.search(text)
    if not (m_t and m_tot):
        return []
    t = _utc(m_t.group(1)) - float(m_tot.group(1))
    out = []
    for _, d, uri, mp in raw:
        out.append((t, d, uri, mp))
        t += d
    return out


class TwitchVodFetcher:
    def __init__(self, bandwidth):
        self.bw = bandwidth
        self.cache: dict[str, VodInfo] = {}
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=True,
                                        headers={"User-Agent": "Mozilla/5.0 clipbot"})
        self.stats: dict[str, dict] = {}
        self.executor = None          # ProcessPoolExecutor set by App (keeps yt-dlp off the GIL)

    async def resolve(self, channel: str, force: bool = False) -> VodInfo:
        vi = self.cache.get(channel)
        if vi and not force and time.time() - vi.resolved_at < 1800:
            return vi
        try:
            vi = await asyncio.get_running_loop().run_in_executor(self.executor, _resolve_sync, channel)
        except VodUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise VodUnavailable(f"resolve failed: {e}") from e
        segs = await self._playlist(vi)
        if not segs:
            raise VodUnavailable("empty playlist")
        lag = time.time() - (segs[-1][0] + segs[-1][1])
        if lag > 180:
            raise VodUnavailable(f"latest VOD is not the live one (ends {lag:.0f}s ago)")
        self.cache[channel] = vi
        L.info("%s VOD %s resolved (lag %.1fs)", channel, vi.vod_id, lag)
        return vi

    async def _playlist(self, vi: VodInfo) -> list[tuple]:
        r = await self.client.get(vi.m3u8)
        if r.status_code in (401, 403):
            raise VodUnavailable(f"playlist HTTP {r.status_code} (sub-only/restricted)")
        r.raise_for_status()
        return parse_playlist(r.text)

    @staticmethod
    async def _remux_ts(src: Path) -> Path:
        """fMP4 fragments carry absolute VOD timestamps (a 30s clip probes as ~9h). Remux to MPEG-TS;
        ffmpeg rebases output to start at 0 by default, so raw clips are uniform zero-based .ts files."""
        dst = src.with_suffix(".ts")
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "mpegts", str(dst), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if p.returncode != 0 or not dst.exists():
            raise VodUnavailable(f"fmp4 remux failed: {err.decode()[:200]}")
        src.unlink(missing_ok=True)
        return dst

    async def fetch(self, channel: str, start_ts: float, end_ts: float, out: Path,
                    max_wait: float = 90) -> dict:
        t_req = time.time()
        vi = await self.resolve(channel)
        deadline = time.time() + max_wait
        while True:
            try:
                segs = await self._playlist(vi)
            except httpx.HTTPStatusError:
                vi = await self.resolve(channel, force=True)
                segs = await self._playlist(vi)
            if segs and segs[-1][0] + segs[-1][1] >= end_ts:
                break
            if time.time() > deadline:
                raise VodUnavailable("VOD did not reach clip end in time")
            await asyncio.sleep(4)
        pick = [s for s in segs if s[0] < end_ts and s[0] + s[1] > start_ts]
        if not pick:
            raise VodUnavailable("window not in VOD (before VOD start?)")
        base = vi.m3u8.rsplit("/", 1)[0]
        muted = any("-muted" in s[2] for s in pick)
        fmp4 = pick[0][3] is not None
        if fmp4:
            out = out.with_suffix(".mp4")      # fMP4 segments need their init segment in front
        nbytes = 0
        written_map = None
        with out.open("wb") as f:
            for _, _, uri, mp in pick:
                parts = []
                if mp and mp != written_map:
                    parts.append(mp)
                    written_map = mp
                parts.append(uri)
                for u in parts:
                    url = u if u.startswith("http") else f"{base}/{u}"
                    async with self.client.stream("GET", url) as r:
                        r.raise_for_status()
                        async for chunk in r.aiter_bytes(256 * 1024):
                            await self.bw.acquire(len(chunk))
                            f.write(chunk)
                            nbytes += len(chunk)
        if fmp4:
            out = await self._remux_ts(out)
        took = time.time() - t_req
        st = self.stats.setdefault(channel, {"ok": 0, "fail": 0, "avg_delay": 0.0})
        st["ok"] += 1
        st["avg_delay"] = round(st["avg_delay"] + (time.time() - end_ts - st["avg_delay"]) / st["ok"], 1)
        return {"path": str(out), "container": "fmp4" if fmp4 else "ts",
                "offset": start_ts - pick[0][0], "duration": end_ts - start_ts, "muted": muted,
                "bytes": nbytes, "segments": len(pick), "vod_id": vi.vod_id, "fetch_s": round(took, 1),
                "ready_delay_s": round(time.time() - end_ts, 1)}
