"""Per-channel chat hype detector. Pure logic (no I/O) so it is unit-testable."""
from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field

LONG_PASTE = 80
# invisible chars chatters append to bypass Twitch's duplicate-message filter (U+034F, ZW*, tag chars)
INVIS = re.compile("[\u034f\u00ad\u180e\u200b-\u200f\u2060-\u2064\ufeff\U000e0000-\U000e007f]")


def norm_text(t: str) -> str:
    t = INVIS.sub("", t).lower().strip()
    t = re.sub(r"(.)\1+", r"\1", t)               # loooool -> lol, meee -> me (so votes/pastes match)
    return re.sub(r"\s+", " ", t)


class Ewma:
    def __init__(self, halflife_s: float, tick_s: float):
        self.alpha = 1 - 0.5 ** (tick_s / halflife_s)
        self.mean: float | None = None
        self.var = 0.0

    def z(self, x: float) -> float:
        if self.mean is None:
            return 0.0
        std = max(math.sqrt(self.var), math.sqrt(max(self.mean, 1.0)), 1.0)
        return (x - self.mean) / std

    def update(self, x: float, slow: bool = False, alpha: float | None = None) -> None:
        if self.mean is None:
            self.mean = x
            return
        a = alpha if alpha is not None else (self.alpha / 4 if slow else self.alpha)
        d = x - self.mean
        self.mean += a * d
        self.var = (1 - a) * (self.var + a * d * d)


@dataclass
class Trigger:
    channel: str
    peak_ts: float
    score: float
    breakdown: dict
    top_messages: list


@dataclass
class ChannelState:
    name: str
    tier: str
    started: float
    msgs: deque = field(default_factory=deque)          # (ts, user, norm, hype, long)
    rate: Ewma | None = None
    uniq: Ewma | None = None
    hype_base: Ewma | None = None
    suppress_until: float = 0.0
    cooldown_until: float = 0.0
    episode: dict | None = None
    audio_z: float = 0.0
    last_score: float = 0.0
    last_breakdown: dict = field(default_factory=dict)
    n_triggers: int = 0
    connected: bool = True
    history: deque = field(default_factory=lambda: deque(maxlen=300))   # (ts, score) for sparkline


class Detector:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        d = cfg["detector"]
        self.d = d
        self.bots = set(b.lower() for b in d["bots"])
        lex = list(dict.fromkeys(norm_text(w) for w in d["hype_lexicon"]))   # same normalisation as messages
        self.lex_words = {w.lower() for w in lex if " " not in w and w.isascii() and w.isalnum()}
        self.lex_other = [w.lower() for w in lex if w.lower() not in self.lex_words]
        self.thresholds = dict(d["threshold"])
        self.channels: dict[str, ChannelState] = {}

    # ---- channel lifecycle -------------------------------------------------
    def add_channel(self, name: str, tier: str, now: float) -> None:
        hl, tick = self.d["ewma_halflife_s"], self.d["tick_s"]
        self.channels[name] = ChannelState(
            name, tier, now, rate=Ewma(hl, tick), uniq=Ewma(hl, tick), hype_base=Ewma(hl, tick)
        )

    def reset_channel(self, name: str, now: float) -> None:
        """Stream (re)started: new baseline + warm-up."""
        st = self.channels[name]
        self.add_channel(name, st.tier, now)

    def set_connected(self, name: str, ok: bool, now: float, mute_s: float = 60) -> None:
        """Chat gap handling: while disconnected the baseline is frozen and no triggers fire;
        after reconnect, triggers stay muted for mute_s (backlog/rate jump is not hype)."""
        st = self.channels.get(name)
        if st is None or st.connected == ok:
            return
        st.connected = ok
        st.episode = None
        if ok:
            st.suppress_until = max(st.suppress_until, now + mute_s)
            st.msgs.clear()

    def set_audio(self, name: str, z: float) -> None:
        if name in self.channels:
            self.channels[name].audio_z = z

    def suppress(self, name: str, until: float) -> None:
        st = self.channels.get(name)
        if st:
            st.suppress_until = max(st.suppress_until, until)

    # ---- ingest ------------------------------------------------------------
    def is_hype(self, norm: str) -> bool:
        toks = re.findall(r"[a-z0-9?]+", norm)
        if any(t in self.lex_words for t in toks):
            return True
        return any(w in norm for w in self.lex_other)

    def on_message(self, channel: str, user: str, text: str, ts: float, kind: str = "msg",
                   notice_id: str = "", tags: dict | None = None) -> None:
        st = self.channels.get(channel)
        if st is None:
            return
        tags = tags or {}
        if kind == "usernotice":
            if notice_id == "raid":
                st.suppress_until = max(st.suppress_until, ts + self.d["raid_suppress_s"])
            elif notice_id == "submysterygift" and int(tags.get("msg-param-mass-gift-count", "1") or 1) >= 10:
                st.suppress_until = max(st.suppress_until, ts + 30)
            return                                   # system events never count
        if user.lower() in self.bots or not text:
            return
        if tags.get("bits") and int(tags["bits"]) >= 500:
            st.suppress_until = max(st.suppress_until, ts + 15)
        n = norm_text(text)
        st.msgs.append((ts, user.lower(), n, self.is_hype(n), len(n) > LONG_PASTE))

    # ---- scoring -----------------------------------------------------------
    def _window_features(self, st: ChannelState, now: float) -> tuple[float, float, float]:
        w = self.d["window_s"]
        cutoff_keep = now - 30
        while st.msgs and st.msgs[0][0] < cutoff_keep:
            st.msgs.popleft()
        win = [m for m in st.msgs if now - w < m[0] <= now]
        if not win:
            return 0.0, 0.0, 0.0
        # copypasta: each distinct text contributes at most 3; long pastes weigh half
        per_text = Counter(m[2] for m in win)
        # vote flood: one non-hype answer dominating chat ("viper", "yoga", "1", "2"...) is a poll, not a moment
        top_text, top_n = per_text.most_common(1)[0]
        vote = top_text if (not self.is_hype(top_text) and len(top_text) <= 20
                            and ((top_n >= 8 and top_n / len(win) >= 0.3) or top_n >= 20)) else None
        rate = 0.0
        seen_text: Counter = Counter()
        per_user: Counter = Counter()
        users: dict[str, float] = {}
        hype = 0
        for ts, u, n, h, lng in win:
            seen_text[n] += 1
            per_user[u] += 1
            pasta = per_text[n] >= 5 and len(n) >= 25          # same longish text from many users
            wgt = 0.05 if pasta else (0.15 if n == vote else (0.5 if lng else 1.0))
            if seen_text[n] <= 3 and per_user[u] <= 2:
                rate += wgt
            users[u] = max(users.get(u, 0.0), wgt)
            hype += h and not pasta
        return rate / w, sum(users.values()) / w, hype / len(win)

    def tick(self, now: float) -> list[Trigger]:
        out = []
        for st in self.channels.values():
            t = self._tick_channel(st, now)
            if t:
                out.append(t)
        return out

    def _tick_channel(self, st: ChannelState, now: float) -> Trigger | None:
        d = self.d
        if not st.connected:
            st.history.append((now, 0.0))
            return None
        rate, uniq, hype_ratio = self._window_features(st, now)
        rz, uz = st.rate.z(rate), st.uniq.z(uniq)
        base_h = st.hype_base.mean or 0.0
        flood = max(0.0, hype_ratio - base_h)
        w = d["weights"]
        score = (w["unique_z"] * max(uz, 0) + w["rate_z"] * max(rz, 0)
                 + w["emote_flood"] * flood + w["audio_spike"] * max(st.audio_z, 0))
        spiking = uz > 2
        warm = now - st.started < d["warmup_s"]
        fast = 1 - 0.5 ** (d["tick_s"] / 20) if warm else None     # converge quickly during warm-up
        st.rate.update(rate, slow=spiking, alpha=fast)
        st.uniq.update(uniq, slow=spiking, alpha=fast)
        st.hype_base.update(hype_ratio, slow=spiking, alpha=fast)
        st.last_score = score
        st.last_breakdown = {"unique_z": round(uz, 2), "rate_z": round(rz, 2), "emote_flood": round(flood, 3),
                             "audio_z": round(st.audio_z, 2), "rate": round(rate, 2), "uniq": round(uniq, 2),
                             "base_rate": round(st.rate.mean or 0, 2)}
        st.history.append((now, round(score, 2)))

        blocked = (now - st.started < d["warmup_s"]) or now < st.suppress_until or now < st.cooldown_until
        thr = self.thresholds[st.tier]
        ep = st.episode
        if score >= thr and not blocked:
            if ep is None:
                st.episode = ep = {"start": now, "peak_ts": now, "peak": score, "bd": st.last_breakdown}
            elif score > ep["peak"]:
                ep.update(peak_ts=now, peak=score, bd=st.last_breakdown)
            if now - ep["start"] < 25:
                return None
        if ep is None:
            return None
        st.episode = None
        if blocked or now - ep["start"] < d["sustain_s"]:
            return None                      # blip (or got suppressed mid-episode)
        st.cooldown_until = now + d["cooldown_s"]
        st.n_triggers += 1
        top = Counter(m[2] for m in st.msgs if ep["peak_ts"] - 8 <= m[0] <= ep["peak_ts"] + 4).most_common(10)
        return Trigger(st.name, ep["peak_ts"], round(ep["peak"], 2), ep["bd"], [f"{t} x{c}" for t, c in top])
