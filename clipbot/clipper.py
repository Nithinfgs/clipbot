"""Trigger -> raw clip file (.ts, uncut-ish) + trim info. Precise trim happens once, in the editor."""
from __future__ import annotations

import asyncio
import json
import time

from . import activity, db, log
from .config import CLIPS
from .recorder import Recorder, seg_ts
from .vodfetch import TwitchVodFetcher, VodUnavailable

L = log.get("clipper")
RAW = CLIPS / "raw"
RAW.mkdir(parents=True, exist_ok=True)


def window(peak_ts: float, cfg: dict, source: str) -> tuple[float, float]:
    c = cfg["clip"]
    shift = c.get("vod_latency_s", 0) if source == "vod" else 0
    return peak_ts - c["pre_s"] - shift, peak_ts + c["post_s"] - shift


class Clipper:
    def __init__(self, cfg, con, recorders: dict[str, Recorder], vod: TwitchVodFetcher, on_vod_fail):
        self.cfg, self.con, self.recorders, self.vod = cfg, con, recorders, vod
        self.on_vod_fail = on_vod_fail
        self.queue: asyncio.Queue = asyncio.Queue()
        self.made = 0

    def submit(self, clip_id: int) -> None:
        self.queue.put_nowait(clip_id)

    async def worker(self) -> None:
        while True:
            cid = await self.queue.get()
            try:
                await self._process(cid)
            except Exception as e:  # noqa: BLE001
                L.exception("clip %s failed", cid)
                activity.record("clip_failed", clip_id=cid, detail=str(e)[:200])
                db.update_clip(self.con, cid, status="failed", error=str(e)[:300])

    async def _process(self, cid: int) -> None:
        row = self.con.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
        wait = row["peak_ts"] + self.cfg["clip"]["wait_after_peak_s"] - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        out = RAW / f"{cid}_{row['streamer']}.ts"
        info = None
        if row["source"] == "live":
            # VOD first (720p60, better quality); live buffer (480p) is the safety net
            sa, sb = window(row["peak_ts"], self.cfg, "vod")
            try:
                info = await self.vod.fetch(row["streamer"], sa, sb, out, max_wait=40)
                info["via"] = "vod"
                db.update_clip(self.con, cid, start_ts=sa, end_ts=sb)
            except Exception as e:  # noqa: BLE001
                L.info("clip %s %s: VOD path failed (%s), using live buffer", cid, row["streamer"], e)
                info = await self._from_buffer(row, out)
                if info:
                    info["via"] = "buffer"
        else:
            try:
                info = await self.vod.fetch(row["streamer"], row["start_ts"], row["end_ts"], out)
            except VodUnavailable as e:
                st = self.vod.stats.setdefault(row["streamer"], {"ok": 0, "fail": 0, "avg_delay": 0.0})
                st["fail"] += 1
                self.on_vod_fail(row["streamer"], str(e))
                db.update_clip(self.con, cid, status="failed", error=f"vod: {e}")
                L.warning("clip %s %s VOD failed: %s", cid, row["streamer"], e)
                return
        if info is None:
            return
        self.made += 1
        raw_path = info.get("path", str(out))
        meta = json.loads(row["meta"] or "{}")
        meta.update(info)                      # keep stream title/start/viewers captured at trigger time
        db.update_clip(self.con, cid, status="raw", raw_path=raw_path, meta=meta)
        activity.record("clip_ready", clip_id=cid, streamer=row["streamer"], duration_s=round(row["end_ts"] - row["start_ts"]),
                        detail=f"{info.get('via', row['source'])} {info.get('container', '')} ready {time.time() - row['end_ts']:.0f}s after moment")
        L.info("clip %s raw ready: %s %s (%s, ready %.0fs after window end)", cid, row["streamer"], out.name,
               row["source"], time.time() - row["end_ts"])

    async def _from_buffer(self, row, out):
        rec = self.recorders.get(row["streamer"])
        a, b = row["start_ts"], row["end_ts"]
        if rec is None:
            db.update_clip(self.con, row["id"], status="failed", error="no recorder")
            return None
        if rec.in_ad(a, b):
            db.update_clip(self.con, row["id"], status="failed", error="overlaps ad break")
            return None
        seg = self.cfg["buffer"]["segment_s"]
        for _ in range(10):                       # wait for the segment covering b to close
            segs = rec.segments()
            if segs and seg_ts(segs[-1]) >= b:
                break
            await asyncio.sleep(2)
        segs = [s for s in rec.segments() if seg_ts(s) < b and seg_ts(s) + seg + 2 > a]
        segs = [s for s in segs if s != rec.segments()[-1]] or segs   # skip the one still being written
        if not segs or seg_ts(segs[0]) > a + 3:
            db.update_clip(self.con, row["id"], status="failed", error="window not in buffer")
            return None
        with out.open("wb") as f:
            for s in segs:
                f.write(s.read_bytes())
        return {"offset": max(a - seg_ts(segs[0]), 0), "duration": b - a, "muted": False,
                "segments": len(segs), "bytes": out.stat().st_size,
                "ready_delay_s": round(time.time() - b, 1)}
