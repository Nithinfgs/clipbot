"""Supervisor: chat + detector + capture (live buffer / VOD) + clipper. Phase 1 of ClipBot."""
from __future__ import annotations

import asyncio
import signal
from concurrent.futures import ProcessPoolExecutor
import time

import yt_dlp

from . import activity, db, log, storage
from .alerts import Alerts
from .bandwidth import Bandwidth
from .chat.twitch import ChatMsg, TwitchChat
from .clipper import Clipper, window
from .config import load_config, load_env
from .detector import Detector
from .pipeline import Pipeline
from .recorder import Recorder
from .roster import Helix, blocked, gta6_by_title, stream_from_helix, tier_for
from .vodfetch import TwitchVodFetcher, VodUnavailable

L = log.get("main")


def _ignore_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _live_sync(name: str) -> tuple[bool, dict]:
    """-> (is_live, {display, title, started, viewers}). title = the streamer's stream title."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "socket_timeout": 15}) as y:
            info = y.extract_info(f"https://www.twitch.tv/{name}", download=False, process=False)
            return bool(info.get("is_live", True)), {
                "display": info.get("uploader") or name, "title": info.get("description") or "",
                "started": info.get("timestamp"), "viewers": info.get("concurrent_view_count")}
    except yt_dlp.utils.DownloadError as e:
        if "not currently live" in str(e) or "does not exist" in str(e):
            return False, {}
        raise


class App:
    def __init__(self):
        load_env()
        self.cfg = load_config()
        self.con = db.connect()
        self.streamers = {s["name"]: s for s in self.cfg["streamers"]}
        self.live: dict[str, bool | None] = {n: None for n in self.streamers}
        self.viewers: dict[str, int | None] = {}
        self.stream_info: dict[str, dict] = {}
        self.chat_ok: dict[str, bool] = {}
        self.vod_fail: dict[str, int] = {}
        self.dropped: set[str] = set()
        self.alerts = Alerts()
        self.bw = Bandwidth(self.cfg["bandwidth"]["cap_mbps"])
        self.det = Detector(self.cfg)
        self.recorders: dict[str, Recorder] = {}
        self.rec_tasks: dict[str, asyncio.Task] = {}
        self.vod = TwitchVodFetcher(self.bw)
        self.clipper = Clipper(self.cfg, self.con, self.recorders, self.vod, self.on_vod_fail)
        self.pool = ProcessPoolExecutor(max_workers=3, initializer=_ignore_sigint)
        self.vod.executor = self.pool
        tw = [n for n, s in self.streamers.items() if s["platform"] == "twitch"]
        per = self.cfg.get("chat", {}).get("channels_per_connection", 5)
        self.chats = [TwitchChat(tw[i:i + per], self.on_chat, self.on_chat_state) for i in range(0, len(tw), per)]
        self.loop_lag_max = 0.0
        self.chat_per = per
        self.helix = Helix.from_env()
        self.gta6: dict[str, bool] = {}            # name -> currently playing GTA 6
        self.inactive_since: dict[str, float] = {}
        self.kw = [k.lower() for k in self.cfg.get("roster", {}).get("gta6_title_keywords", [])]
        now = time.time()
        for n, s in self.streamers.items():
            self.det.add_channel(n, s["tier"], now)
        self.pipeline = Pipeline(self)
        stale = self.con.execute("UPDATE clips SET status='failed', error='interrupted (restart)' "
                                 "WHERE status='pending'").rowcount
        if stale:
            L.info("marked %d interrupted pending clips as failed", stale)

    # ---- alerts -------------------------------------------------------------
    def alert(self, key: str, msg: str) -> None:
        if self.alerts.send(key, msg) or not self.alerts.topic:
            db.insert(self.con, "events", ts=time.time(), level="ALERT", component=key, msg=msg)

    # ---- chat ---------------------------------------------------------------
    def on_chat(self, m: ChatMsg) -> None:
        self.det.on_message(m.channel, m.user, m.text, m.ts, m.kind, m.notice_id, m.tags)

    def on_chat_state(self, ch: str, ok: bool) -> None:
        self.chat_ok[ch] = ok
        self.det.set_connected(ch, ok, time.time())

    # ---- capture management -------------------------------------------------
    def start_recorder(self, name: str) -> None:
        if name in self.recorders:
            return
        s = self.streamers[name]
        rec = Recorder(name, f"https://www.twitch.tv/{name}", s.get("quality") or self.cfg["bandwidth"]["live_quality"],
                       self.cfg["buffer"]["segment_s"], self.cfg["buffer"]["keep_s"], self.alert,
                       lambda n=name: self.live.get(n))
        self.recorders[name] = rec
        self.rec_tasks[name] = asyncio.create_task(rec.run())
        s["capture"] = "live"
        L.info("recorder started for %s", name)

    def on_vod_fail(self, name: str, reason: str) -> None:
        self.vod_fail[name] = self.vod_fail.get(name, 0) + 1
        hard = any(k in reason for k in ("sub-only", "no archive", "restricted", "not the live one", "no playable"))
        if not hard and self.vod_fail[name] < 2:
            return
        n_live = sum(1 for n in self.recorders if self.live.get(n))
        if n_live < self.cfg["bandwidth"]["max_live_recorders"] + 3 and self.bw.headroom_mbps() > 5:
            L.warning("%s: VOD unusable (%s) -> falling back to live recording", name, reason)
            self.start_recorder(name)
        else:
            L.warning("%s: VOD unusable (%s) and no bandwidth for live -> dropped", name, reason)
            self.dropped.add(name)

    async def poll_live(self) -> None:
        sem = asyncio.Semaphore(3)
        loop = asyncio.get_running_loop()

        async def one(name):
            async with sem:
                try:
                    is_live, sinfo = await loop.run_in_executor(self.pool, _live_sync, name)
                except Exception as e:  # noqa: BLE001
                    L.debug("live check %s failed: %s", name, e)
                    return
            prev = self.live.get(name)
            self.live[name] = is_live
            self.viewers[name] = sinfo.get("viewers")
            if sinfo:
                self.stream_info[name] = {**self.stream_info.get(name, {}), **sinfo}
                if not self.helix:
                    self.gta6[name] = gta6_by_title(sinfo.get("title", ""), self.cfg)
            if is_live and not prev:
                L.info("%s is LIVE", name)
                if prev is False:
                    self.det.reset_channel(name, time.time())
                s = self.streamers[name]
                if s["capture"] == "vod" and name not in self.dropped:
                    try:
                        await self.vod.resolve(name, force=True)
                    except VodUnavailable as e:
                        self.on_vod_fail(name, str(e))
            elif prev and not is_live:
                L.info("%s went offline", name)

        while True:
            await asyncio.gather(*(one(n) for n in list(self.streamers)))
            await asyncio.sleep(60)

    # ---- detection loop -----------------------------------------------------
    async def detect_loop(self) -> None:
        tick = self.cfg["detector"]["tick_s"]
        nxt = time.time()
        while True:
            nxt += tick
            await asyncio.sleep(max(nxt - time.time(), 0))
            now = time.time()
            for name, rec in self.recorders.items():
                if now < rec.ad_until:
                    self.det.suppress(name, rec.ad_until)
            for t in self.det.tick(now - 1.5):     # small lag so late-arriving chat is counted
                self.on_trigger(t)

    def is_active(self, name: str) -> bool:
        """Owner rule: clip only streams that are live AND playing GTA 6."""
        if not self.live.get(name) or name in self.dropped:
            return False
        if not self.cfg.get("roster", {}).get("require_gta6", True):
            return True
        return self.gta6.get(name, False)

    def on_trigger(self, t) -> None:
        s = self.streamers.get(t.channel)
        if s is None or not self.is_active(t.channel):
            L.info("skip trigger %s (not live on GTA 6)", t.channel)
            return
        tid = db.insert(self.con, "triggers", streamer=t.channel, tier=s["tier"], peak_ts=t.peak_ts, score=t.score,
                        breakdown=t.breakdown, top_messages=t.top_messages, created_at=time.time())
        source = "live" if t.channel in self.recorders else "vod"
        a, b = window(t.peak_ts, self.cfg, source)
        cid = db.insert(self.con, "clips", trigger_id=tid, streamer=t.channel, platform=s["platform"], tier=s["tier"],
                        peak_ts=t.peak_ts, start_ts=a, end_ts=b, source=source, status="pending",
                        meta={"viewers": self.viewers.get(t.channel), "stream": self.stream_info.get(t.channel, {})},
                        created_at=time.time(),
                        updated_at=time.time())
        si = self.stream_info.get(t.channel, {})
        activity.record("trigger", clip_id=cid, streamer=t.channel, stream_title=si.get("title"),
                        moment_time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.peak_ts)), score=t.score,
                        detail=" | ".join(t.top_messages[:5]))
        log.kv(L, 20, f"TRIGGER {t.channel} score={t.score} {t.breakdown} top={t.top_messages[:5]}",
               event="trigger", streamer=t.channel, score=t.score, breakdown=t.breakdown, top=t.top_messages,
               clip_id=cid)
        self.clipper.submit(cid)

    # ---- roster: keep ~target live GTA 6 channels, fill with smaller ones ----
    async def chat_add(self, name: str) -> None:
        for c in self.chats:
            if len(c.channels) < self.chat_per:
                await c.join(name)
                return
        c = TwitchChat([name], self.on_chat, self.on_chat_state)
        self.chats.append(c)
        asyncio.create_task(c.run())

    async def add_streamer(self, login: str, sinfo: dict) -> bool:
        tier = tier_for(sinfo.get("viewers") or 0, self.cfg)
        try:
            await self.vod.resolve(login, force=True)
            capture = "live" if tier == "A" else "vod"
        except VodUnavailable as e:
            if tier != "A":
                L.info("roster: skip %s (no usable VOD: %s)", login, e)
                self.dropped.add(login)
                return False
            capture = "live"
        s = {"name": login, "platform": "twitch", "tier": tier, "capture": capture, "dynamic": True}
        self.streamers[login] = s
        self.live[login] = True
        self.gta6[login] = True
        self.stream_info[login] = sinfo
        self.viewers[login] = sinfo.get("viewers")
        self.det.add_channel(login, tier, time.time())
        await self.chat_add(login)
        if capture == "live" and self.bw.headroom_mbps() > 5:
            self.start_recorder(login)
        L.info("roster: + %s (tier %s, %s viewers, %s)", login, tier, sinfo.get("viewers"), capture)
        activity.record("roster_add", streamer=login, stream_title=sinfo.get("title"),
                        detail=f"tier {tier}, {sinfo.get('viewers')} viewers, {capture}")
        return True

    async def remove_streamer(self, name: str, why: str) -> None:
        s = self.streamers.pop(name, None)
        if not s:
            return
        for c in self.chats:
            if name in c.channels:
                await c.part(name)
        self.det.channels.pop(name, None)
        rec = self.recorders.pop(name, None)
        if rec:
            await rec.stop()
        self.inactive_since.pop(name, None)
        L.info("roster: - %s (%s)", name, why)
        activity.record("roster_remove", streamer=name, detail=why)

    async def roster_loop(self) -> None:
        r = self.cfg.get("roster", {})
        target, min_v = r.get("target_active", 40), r.get("min_viewers", 300)
        warned = False
        await asyncio.sleep(20)
        while True:
            try:
                now = time.time()
                listing = []
                if self.helix:
                    listing = await self.helix.gta6_streams(r.get("language", "en"))
                    on_gta = {s["user_login"].lower(): s for s in listing}
                    others = [n for n in self.streamers if n not in on_gta]
                    extra = await self.helix.streams_for(others) if others else {}
                    for n in list(self.streamers):
                        h = on_gta.get(n) or extra.get(n)
                        self.gta6[n] = bool(h and h.get("game_name") == "Grand Theft Auto VI")
                        if h:
                            self.live[n] = True
                            self.stream_info[n] = {**self.stream_info.get(n, {}), **stream_from_helix(h)}
                elif not warned:
                    L.warning("roster: no TWITCH_CLIENT_ID/SECRET -> GTA 6 check by stream title only, no discovery")
                    warned = True
                active = [n for n in self.streamers if self.is_active(n)]
                for n in list(self.streamers):        # park dynamic channels that left GTA 6 / went offline
                    if self.is_active(n):
                        self.inactive_since.pop(n, None)
                    else:
                        since = self.inactive_since.setdefault(n, now)
                        if self.streamers[n].get("dynamic") and now - since > 900:
                            await self.remove_streamer(n, "not live on GTA 6 for 15 min")
                need = target - len(active)
                added = 0
                if need > 0 and listing:
                    for s in listing:                 # highest viewers first
                        login = s["user_login"].lower()
                        if added >= min(need, 8):
                            break
                        if login in self.streamers or login in self.dropped or blocked(login, self.cfg):
                            continue
                        if (s.get("viewer_count") or 0) < min_v:
                            break
                        if await self.add_streamer(login, stream_from_helix(s)):
                            added += 1
                L.info("ROSTER active=%d/%d target=%d added=%d (big ones off/away: %s)", len(active),
                       len(self.streamers), target, added,
                       ",".join(n for n, s in self.streamers.items() if s["tier"] == "A" and not self.is_active(n))[:200])
            except Exception as e:  # noqa: BLE001
                L.warning("roster refresh failed: %s", e)
            await asyncio.sleep(r.get("refresh_s", 120))

    async def storage_loop(self) -> None:
        st = self.cfg.get("storage", {})
        while True:
            protected = {r[0] for r in self.con.execute(
                "SELECT raw_path FROM clips WHERE status IN ('pending','raw','editing') AND raw_path IS NOT NULL "
                "UNION SELECT final_path FROM clips WHERE status IN ('ready','posting') AND final_path IS NOT NULL")}
            await asyncio.to_thread(
                storage.enforce, st.get("clips_max_gb", 60), st.get("min_free_gb", 15), protected,
                on_delete=lambda p, size: activity.record("deleted_old_clip", detail=f"{p.name} {size/1e6:.1f}MB"))
            await asyncio.sleep(60)

    async def status_post_only(self) -> None:
        while True:
            await asyncio.sleep(300)
            counts = dict(self.con.execute("SELECT status, count(*) FROM clips GROUP BY status").fetchall())
            L.info("STATUS post-only: ready=%s posted=%s", counts.get("ready"), counts.get("posted"))

    async def lag_monitor(self) -> None:
        """Event-loop health: a blocked loop starves websocket keepalives (caused chat drops)."""
        while True:
            t = time.monotonic()
            await asyncio.sleep(0.5)
            lag = time.monotonic() - t - 0.5
            self.loop_lag_max = max(self.loop_lag_max, lag)
            if lag > 1.0:
                L.warning("event loop blocked for %.2fs", lag)

    # ---- status -------------------------------------------------------------
    async def status_loop(self, every: int = 60) -> None:
        while True:
            await asyncio.sleep(every)
            live = [n for n in self.streamers if self.is_active(n)]
            rec_ok = sum(r.healthy for r in self.recorders.values())
            counts = dict(self.con.execute("SELECT status, count(*) FROM clips GROUP BY status").fetchall())
            scores = " ".join(f"{n}:{st.last_score:.1f}" for n, st in self.det.channels.items() if self.live.get(n))
            L.info("STATUS live=%d/%d rec=%d/%d chat=%d/%d lag_max=%.2fs bw iface=%.1f live=%.1f vod=%.1f Mbps clips=%s | %s",
                   len(live), len(self.streamers), rec_ok, len(self.recorders),
                   sum(c.connected for c in self.chats), len(self.chats), self.loop_lag_max,
                   self.bw.iface_mbps, self.bw.live_mbps, self.bw.vod_mbps, counts, scores)
            self.loop_lag_max = 0.0
            for n, st in self.det.channels.items():
                db.set_health(self.con, f"stream:{n}", bool(self.live.get(n)),
                              f"score={st.last_score:.1f} trig={st.n_triggers}")

    async def run(self) -> None:
        if self.cfg.get("mode") == "post_only":
            # owner 2026-11-19 07:15: stop watching streams, keep posting the finished backlog
            activity.record("run_start", detail="post-only mode: no streams, posting finished backlog")
            L.info("POST-ONLY mode: no chat/recording/editing; posting finished Reels")
            await asyncio.gather(self.pipeline.scheduler(), self.pipeline.hourly(), self.status_post_only())
            return
        activity.record("run_start", detail=f"{len(self.streamers)} streamers, {len(self.cfg.get('accounts', []))} accounts")
        for n, s in self.streamers.items():
            if s["capture"] == "live":
                self.start_recorder(n)
        tasks = [c.run() for c in self.chats]
        tasks += [self.poll_live(), self.detect_loop(), self.bw.run(), self.status_loop(), self.lag_monitor(),
                  self.storage_loop(), self.roster_loop()]
        tasks += self.pipeline.tasks()
        tasks += [self.clipper.worker() for _ in range(3)]
        await asyncio.gather(*tasks)

    async def shutdown(self) -> None:
        for r in self.recorders.values():
            await r.stop()
        for t in self.rec_tasks.values():
            t.cancel()
        await asyncio.gather(*self.rec_tasks.values(), return_exceptions=True)
        self.pool.shutdown(wait=False, cancel_futures=True)


def main() -> None:
    log.setup()
    app = App()
    loop = asyncio.new_event_loop()
    main_task = loop.create_task(app.run())

    def stop(*_):
        L.info("shutting down…")
        main_task.cancel()

    loop.add_signal_handler(signal.SIGINT, stop)
    loop.add_signal_handler(signal.SIGTERM, stop)
    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.run_until_complete(app.shutdown())
        L.info("stopped")


if __name__ == "__main__":
    main()
