"""raw clip -> (assign account) -> edit -> gates -> ready -> scheduled slot -> published Reel.

Status flow in `clips`: pending -> raw -> editing -> ready | rejected | stale -> posting -> posted | post_failed
Owner authorisation (2026-11-19): auto-post to all 5 accounts.

Posting rules
  * slots per account: start + offset + k*interval; brand-new accounts: 30 min for first 3 h, then 16 min
    (16 min x 12 h = 45 posts = our 24 h safety cap; API quota is 100)
  * at the slot, post the best ready clip for that account (score x tier x freshness); skip slot if none
  * ready clips older than 75 min go stale (GTA moments age fast)
  * auth / blocked / rate / limit errors pause that account (blocked/auth: for the run) + phone alert;
    other accounts keep posting. Clip-level errors just fail that clip.
  * idempotent: row marked 'posting' before upload; on restart 'posting' rows are reconciled against the
    account's recent media by caption before anything is retried
  * kill switch: data/PAUSE (all) or data/PAUSE_acc3 (one account)  ->  ./ctl pause / resume
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from . import activity, db, log
from .captions import build_caption, is_clean
from .config import CLIPS, DATA
from .editor import edit, frame_stats, hook_from_chat, probe
from .gates import check, choose_account, overlaps, vibe_scores
from .publisher import Publisher, PublishError, accounts_from_env

L = log.get("pipeline")
FINAL = CLIPS / "final"
TMP = CLIPS / "tmp"
TIER_W = {"A": 1.3, "B": 1.1, "C": 1.0}

MIGRATIONS = ["final_path TEXT", "caption TEXT", "thumb_ms INTEGER", "gate TEXT", "rank REAL", "hook TEXT",
              "ready_at REAL", "posted_at REAL", "permalink TEXT", "media_id TEXT", "container_id TEXT",
              "edit_meta TEXT"]


def migrate(con) -> None:
    have = {r[1] for r in con.execute("PRAGMA table_info(clips)")}
    for col in MIGRATIONS:
        if col.split()[0] not in have:
            con.execute(f"ALTER TABLE clips ADD COLUMN {col}")


class Pipeline:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.con = app.con
        migrate(self.con)
        pc = self.cfg.get("posting", {})
        self.enabled = pc.get("enabled", True)
        self.run_hours = self.cfg["run"].get("hours", 12)
        self.new_interval = pc.get("new_account_interval_min", 30) * 60
        self.ramp_s = pc.get("new_account_ramp_h", 3) * 3600
        self.interval = pc.get("interval_min", 16) * 60
        self.cap24 = pc.get("cap_per_24h", 45)
        self.stale_s = pc.get("stale_min", 75) * 60
        self.accounts = {a["name"]: a for a in self.cfg["accounts"]}
        self.pubs = {a.name: Publisher(a) for a in accounts_from_env(self.cfg["accounts"])}
        self.paused: dict[str, float] = {}           # account -> paused until (inf = rest of run)
        self.fail_streak: dict[str, int] = {}
        self.last_tags: dict[str, set] = {}
        self.edit_q: asyncio.Queue = asyncio.Queue()
        self.whisper_lock = asyncio.Lock()
        self.queued: set[int] = set()
        start_file = DATA / "run_start"
        if not start_file.exists():
            start_file.write_text(str(time.time()))
        self.run_start = float(start_file.read_text())
        self.next_slot: dict[str, float] = {}
        self.stats = {"edited": 0, "rejected": 0, "posted": 0, "post_failed": 0, "skipped_slots": 0}

    # ---------------------------------------------------------------- helpers
    def _row(self, cid):
        return self.con.execute("SELECT c.*, t.top_messages, t.score FROM clips c LEFT JOIN triggers t "
                                "ON t.id=c.trigger_id WHERE c.id=?", (cid,)).fetchone()

    def ready_counts(self) -> dict[str, int]:
        return dict(self.con.execute("SELECT account, count(*) FROM clips WHERE status IN ('ready','editing') "
                                     "GROUP BY account").fetchall())

    def recent_by_account(self) -> dict[str, list[tuple]]:
        out: dict[str, list[tuple]] = {}
        for acc, st, ts in self.con.execute(
                "SELECT account, streamer, coalesce(posted_at, ready_at, created_at) FROM clips "
                "WHERE account IS NOT NULL AND status IN ('editing','ready','posting','posted') "
                "AND coalesce(posted_at, ready_at, created_at) > ? ORDER BY 3", (time.time() - 7200,)):
            out.setdefault(acc, []).append((st, ts))
        return out

    def is_paused(self, acc: str) -> bool:
        if (DATA / "PAUSE").exists() or (DATA / f"PAUSE_{acc}").exists():
            return True
        return time.time() < self.paused.get(acc, 0)

    def account_age_s(self, acc: str) -> float:
        a = self.accounts[acc]
        return time.time() - a.get("created_ts", self.run_start)

    def slot_interval(self, acc: str) -> float:
        return self.new_interval if time.time() - self.run_start < self.ramp_s else self.interval

    # ---------------------------------------------------------------- editing
    async def feeder(self) -> None:
        """Queue fresh raw clips (newest first) for editing; expire old/stale ones."""
        while True:
            now = time.time()
            self.con.execute("UPDATE clips SET status='stale' WHERE status='raw' AND created_at < ?", (now - 1800,))
            n = self.con.execute("UPDATE clips SET status='stale' WHERE status='ready' AND ready_at < ?",
                                 (now - self.stale_s,)).rowcount
            if n:
                L.info("expired %d stale ready clips", n)
            rows = self.con.execute("SELECT id FROM clips WHERE status='raw' ORDER BY created_at DESC LIMIT 20").fetchall()
            for (cid,) in rows:
                if cid not in self.queued:
                    self.queued.add(cid)
                    self.edit_q.put_nowait(cid)
            await asyncio.sleep(5)

    async def editor_worker(self, wid: int) -> None:
        while True:
            cid = await self.edit_q.get()
            try:
                await self._edit_one(cid)
            except Exception as e:  # noqa: BLE001
                L.exception("edit %s failed", cid)
                db.update_clip(self.con, cid, status="rejected", gate=f"edit error: {str(e)[:200]}")
                activity.record("rejected", clip_id=cid, detail=f"edit error: {str(e)[:150]}")
            finally:
                self.queued.discard(cid)

    async def _edit_one(self, cid: int) -> None:
        row = self._row(cid)
        if row is None or row["status"] != "raw" or not row["raw_path"] or not Path(row["raw_path"]).exists():
            return
        streamer = row["streamer"]
        # duplicate moment: overlaps a clip from the same streamer already ready/posted
        for o in self.con.execute("SELECT start_ts, end_ts FROM clips WHERE streamer=? AND id!=? AND status IN "
                                  "('editing','ready','posting','posted')", (streamer, cid)):
            if overlaps(row["start_ts"], row["end_ts"], o[0], o[1]):
                db.update_clip(self.con, cid, status="rejected", gate="duplicate moment")
                return
        top = json.loads(row["top_messages"] or "[]")
        score = row["score"] or 0
        vibes = vibe_scores(top)
        # assignment skips accounts that are API-paused / individually paused; the global PAUSE switch only
        # stops posting (editing keeps a buffer ready)
        paused = {a for a in self.accounts if a not in self.pubs or time.time() < self.paused.get(a, 0)
                  or (DATA / f"PAUSE_{a}").exists()}
        acc = choose_account(clip_streamer=streamer, tier=row["tier"], score=score, vibes=vibes,
                             accounts=list(self.accounts.values()), ready_count=self.ready_counts(),
                             recent_posts=self.recent_by_account(), paused=paused, now=time.time())
        if acc is None:
            db.update_clip(self.con, cid, status="rejected", gate="no eligible account (variety rules)")
            return
        a = self.accounts[acc]
        db.update_clip(self.con, cid, status="editing", account=acc)
        meta = json.loads(row["meta"] or "{}")
        stream = meta.get("stream", {})
        display = stream.get("display") or streamer
        hook = hook_from_chat(top, display, seed=cid)
        out = FINAL / f"{cid}_{a['handle']}.mp4"
        async with self.whisper_lock:     # whisper model is not re-entrant; ffmpeg encode is the long part anyway
            r = await asyncio.to_thread(edit, Path(row["raw_path"]), meta.get("offset", 0), meta.get("duration", 30),
                                        self.cfg["clip"]["pre_s"], a["handle"], a["color"], hook, out, TMP)
        p = await asyncio.to_thread(probe, out)
        fs = await asyncio.to_thread(frame_stats, out)
        st = self.app.streamers.get(streamer, {"name": streamer})
        try:
            cap = build_caption(hook=hook, streamer=st, stream=stream, moment_ts=row["peak_ts"], account=a,
                                tz=self.cfg["run"]["tz"], last_tags=self.last_tags.get(acc),
                                rng=random.Random(cid))
            clean = is_clean(cap["caption"])
        except ValueError:
            cap, clean = {"caption": "", "hashtags": []}, False
        bad = check({"speech_ratio": r.speech_ratio, "loud_ratio": r.loud_ratio}, p, fs, clean)
        age_min = (time.time() - row["peak_ts"]) / 60
        rank = round(score * TIER_W.get(row["tier"], 1.0) * (1 + min(r.speech_ratio, 0.6)), 2)
        em = {"dur": r.duration, "words": r.words, "speech": r.speech_ratio, "loud": r.loud_ratio, "frames": fs,
              "took_s": r.took_s, "transcript": r.transcript[:300], "hashtags": cap.get("hashtags", [])}
        if bad:
            db.update_clip(self.con, cid, status="rejected", gate="; ".join(bad), final_path=str(out), edit_meta=em)
            self.stats["rejected"] += 1
            activity.record("rejected", clip_id=cid, streamer=streamer, account=a["handle"], detail="; ".join(bad))
            L.info("clip %s %s REJECTED: %s", cid, streamer, "; ".join(bad))
            return
        db.update_clip(self.con, cid, status="ready", final_path=str(out), caption=cap["caption"],
                       thumb_ms=r.thumb_offset_ms, rank=rank, hook=hook, ready_at=time.time(), edit_meta=em)
        self.stats["edited"] += 1
        activity.record("edited", clip_id=cid, streamer=streamer, stream_title=stream.get("title"), account=a["handle"],
                        score=rank, duration_s=r.duration, caption_preview=cap["caption"][:120],
                        detail=f"ready {age_min:.1f} min after moment, render {r.took_s}s")
        L.info("clip %s %s -> @%s READY (rank %.1f, %.0fs, %d words, %.1f min after moment)", cid, streamer,
               a["handle"], rank, r.duration, r.words, age_min)

    # ---------------------------------------------------------------- posting
    def posted_24h(self, acc: str) -> int:
        return self.con.execute("SELECT count(*) FROM clips WHERE account=? AND status='posted' AND posted_at > ?",
                                (acc, time.time() - 86400)).fetchone()[0]

    def pick(self, acc: str):
        now = time.time()
        rows = self.con.execute("SELECT * FROM clips WHERE status='ready' AND account=?", (acc,)).fetchall()
        if not rows:
            return None
        last = self.con.execute("SELECT streamer FROM clips WHERE account=? AND status='posted' ORDER BY posted_at "
                                "DESC LIMIT 1", (acc,)).fetchone()
        # cross-account variety: each post of this streamer on ANY account in the last hour halves its score
        recent = dict(self.con.execute("SELECT streamer, count(*) FROM clips WHERE status='posted' AND posted_at>? "
                                       "GROUP BY streamer", (now - 3600,)).fetchall())
        posted = self.con.execute("SELECT streamer, start_ts, end_ts FROM clips WHERE status IN ('posted','posting')").fetchall()
        rows = [r for r in rows if not any(p[0] == r["streamer"] and overlaps(r["start_ts"], r["end_ts"], p[1], p[2])
                                           for p in posted)]          # never post the same moment twice
        if not rows:
            return None
        very_recent = {x[0] for x in self.con.execute(
            "SELECT DISTINCT streamer FROM clips WHERE (status='posted' AND posted_at>?) OR status='posting'",
            (now - 1200,))}                                   # include in-flight posts on other accounts
        if any(r["streamer"] not in very_recent for r in rows):      # hard rule while alternatives exist
            rows = [r for r in rows if r["streamer"] not in very_recent]
        best, bs = None, -1
        for r in rows:
            if last and r["streamer"] == last[0] and len(rows) > 1:
                continue
            fresh = 0.5 ** ((now - r["peak_ts"]) / 3600)          # 60-min half-life
            s = (r["rank"] or 1) * fresh * 0.5 ** recent.get(r["streamer"], 0)
            if s > bs:
                best, bs = r, s
        return best or rows[0]

    async def reconcile(self) -> None:
        """Crash safety: any row left in 'posting' -> check the account before retrying."""
        for r in self.con.execute("SELECT * FROM clips WHERE status='posting'").fetchall():
            pub = self.pubs.get(r["account"])
            if not pub:
                continue
            try:
                m = await pub.find_posted(r["caption"] or "")
            except PublishError:
                continue
            if m:
                db.update_clip(self.con, r["id"], status="posted", permalink=m.get("permalink"), media_id=m.get("id"),
                               posted_at=time.time())
                L.info("reconcile: clip %s was already posted -> %s", r["id"], m.get("permalink"))
            else:
                db.update_clip(self.con, r["id"], status="ready")

    async def post(self, acc: str, r) -> None:
        pub = self.pubs[acc]
        handle = self.accounts[acc]["handle"]
        db.update_clip(self.con, r["id"], status="posting")
        try:
            used, total = await pub.quota()
            if used >= min(total - 5, self.cap24) or self.posted_24h(acc) >= self.cap24:
                db.update_clip(self.con, r["id"], status="ready")
                self.paused[acc] = time.time() + 3600
                self.app.alert(f"cap:{acc}", f"@{handle} reached the daily post cap ({used}/{total}) - pausing 1h")
                return
            m = await pub.find_posted(r["caption"])
            if m:      # an earlier "failed" publish actually went live -> record it, never post twice
                db.update_clip(self.con, r["id"], status="posted", permalink=m.get("permalink"), media_id=m.get("id"),
                               posted_at=time.time())
                activity.record("posted", clip_id=r["id"], streamer=r["streamer"], account=handle,
                                permalink=m.get("permalink"), detail="recovered: earlier publish had succeeded")
                L.info("POSTED (recovered) clip %s -> @%s %s", r["id"], handle, m.get("permalink"))
                return
            cid_c, _ = await pub.prepare(Path(r["final_path"]), r["caption"], r["thumb_ms"] or 0)
            db.update_clip(self.con, r["id"], container_id=cid_c)
            res = await pub.finish(cid_c, None)
        except PublishError as e:
            self.fail_streak[acc] = self.fail_streak.get(acc, 0) + 1
            if e.pauses_account:
                until = float("inf") if e.kind in ("auth", "blocked") else time.time() + 3600
                self.paused[acc] = until
                db.update_clip(self.con, r["id"], status="ready")
                self.app.alert(f"pause:{acc}", f"⛔ @{handle} paused ({e.kind}): {e.msg[:150]}. Other accounts keep posting.")
                activity.record("account_paused", account=handle, detail=f"{e.kind}: {e.msg[:150]}")
            elif e.kind == "transient" and self.fail_streak[acc] <= 3:
                # Meta hiccup ("unexpected error, retry later"): keep the clip, retry this account in 2 min
                db.update_clip(self.con, r["id"], status="ready")
                self.next_slot[acc] = time.time() + 120
                activity.record("post_retry", clip_id=r["id"], account=handle, detail=e.msg[:150])
            else:
                db.update_clip(self.con, r["id"], status="post_failed", gate=f"post: {e.msg[:200]}")
                activity.record("post_failed", clip_id=r["id"], streamer=r["streamer"], account=handle, detail=e.msg[:200])
                self.stats["post_failed"] += 1
                if self.fail_streak[acc] >= 2:
                    self.app.alert(f"postfail:{acc}", f"@{handle}: 2 posts failed in a row - last: {e.msg[:120]}")
            L.warning("post %s -> @%s failed: %s", r["id"], handle, e)
            return
        self.fail_streak[acc] = 0
        db.update_clip(self.con, r["id"], status="posted", permalink=res.permalink, media_id=res.media_id,
                       posted_at=time.time())
        em = json.loads(r["edit_meta"] or "{}")
        self.last_tags[acc] = set(em.get("hashtags", []))
        self.stats["posted"] += 1
        activity.record("posted", clip_id=r["id"], streamer=r["streamer"], account=handle, permalink=res.permalink,
                        score=r["rank"], duration_s=em.get("dur"), caption_preview=(r["caption"] or "")[:120],
                        detail=f"{(time.time() - r['peak_ts']) / 60:.0f} min after moment")
        L.info("POSTED clip %s %s -> @%s %s", r["id"], r["streamer"], handle, res.permalink)

    async def scheduler(self) -> None:
        n = self.con.execute("UPDATE clips SET status='raw' WHERE status='editing'").rowcount
        if n:
            L.info("requeued %d clips interrupted mid-edit", n)
        await self.reconcile()
        now = time.time()
        for name, a in self.accounts.items():
            last = self.con.execute("SELECT max(posted_at) FROM clips WHERE account=? AND status='posted'",
                                    (name,)).fetchone()[0]
            first = now + 60 * a.get("slot_offset_min", 0) / 3 + 90          # staggered start
            self.next_slot[name] = max(first, (last or 0) + self.slot_interval(name))
        busy: set[str] = set()
        while True:
            now = time.time()
            if now - self.run_start > self.run_hours * 3600:
                L.info("run window (%sh) finished - posting stopped, clips kept", self.run_hours)
                activity.record("run_stop", detail=f"{self.run_hours}h window complete")
                return
            for name in self.accounts:
                if name in busy or name not in self.pubs or now < self.next_slot[name]:
                    continue
                if not self.enabled or self.is_paused(name):
                    continue
                r = self.pick(name)
                if r is None:
                    self.stats["skipped_slots"] += 1
                    self.next_slot[name] = now + 120          # retry soon when a clip becomes ready
                    continue
                self.next_slot[name] = now + self.slot_interval(name)
                db.update_clip(self.con, r["id"], status="posting")   # claim now so other accounts this tick see it
                busy.add(name)

                async def go(n=name, row=r):
                    try:
                        await self.post(n, row)
                    finally:
                        busy.discard(n)
                asyncio.create_task(go())
            await asyncio.sleep(10)

    # ---------------------------------------------------------------- reporting
    def summary(self) -> str:
        lines = []
        for name, a in self.accounts.items():
            n = self.con.execute("SELECT count(*) FROM clips WHERE account=? AND status='posted' AND posted_at>?",
                                 (name, self.run_start)).fetchone()[0]
            rd = self.con.execute("SELECT count(*) FROM clips WHERE account=? AND status='ready'", (name,)).fetchone()[0]
            flag = " ⛔paused" if self.is_paused(name) else ""
            lines.append(f"@{a['handle']}: {n} posted, {rd} ready{flag}")
        hrs = (time.time() - self.run_start) / 3600
        active = 0 if self.cfg.get("mode") == "post_only" else sum(1 for n in self.app.streamers if self.app.is_active(n))
        return (f"ClipBot {hrs:.1f}h in · {active} live GTA 6 streams\n" + "\n".join(lines) +
                f"\nedited {self.stats['edited']} · rejected {self.stats['rejected']} · failed posts "
                f"{self.stats['post_failed']} · empty slots {self.stats['skipped_slots']}")

    async def hourly(self) -> None:
        await asyncio.sleep(600)
        self.app.alerts.send("hourly0", "✅ ClipBot is running\n" + self.summary(), priority="default")
        while True:
            await asyncio.sleep(3600)
            self.app.alerts.send(f"hourly{int(time.time() // 3600)}", self.summary(), priority="low")
            if sum(self.ready_counts().values()) == 0:
                self.app.alert("empty", "No ready clips on any account for a while - check detection/editing")

    def tasks(self) -> list:
        return [self.feeder(), self.editor_worker(0), self.editor_worker(1), self.scheduler(), self.hourly()]
