"""Reel description builder — community rules (2026-11-19):
  * mention the creator, which stream, and when; tag them
  * any hashtags allowed
  * NOTHING vulgar or explicit in the description (the video itself may be anything)
Tagging: @handle + Reel user tag only when the streamer's Instagram handle is verified in config
(`ig:`); otherwise credit as twitch.tv/<name> so we never tag the wrong person.
"""
from __future__ import annotations

import datetime as dt
import random
import re
from zoneinfo import ZoneInfo

# whole-word / stem matches; leetspeak-ish normalisation applied first
_BAD = [
    r"f+u+c+k\w*", r"f+k+", r"fck\w*", r"wtf", r"stfu", r"lmfao+", r"sh+i+t+\w*", r"b+i+t+c+h\w*", r"cunt\w*",
    r"d+i+c+k+(s|head|heads|hole)?", r"cock(s|sucker)?", r"puss(y|ies)", r"porn\w*", r"nsfw", r"sex\w*", r"sexo", r"nud(e|es|ity)",
    r"naked", r"cum(s|ming|shot|med)?", r"horny", r"slut\w*", r"whore\w*", r"hoe(s)?", r"thot\w*", r"tits?", r"titties",
    r"boob\w*", r"onlyfans", r"xxx", r"milf\w*", r"penis(es)?", r"vagina(s)?", r"ass(hole)?s?", r"bastard\w*",
    r"nigg\w*", r"nig+a\w*", r"fag\w*", r"retard\w*", r"rape\w*", r"kys", r"18\+", r"gooner?\w*", r"goon(ing)?",
    r"pedo\w*", r"nazi\w*", r"hitler", r"kill\s*(yo)?urself",
]
_BAD_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(_BAD) + r")(?![a-z0-9])", re.I)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s", "!": "i"})
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍⬀-⯿]+")


def bad_words(text: str) -> list[str]:
    t = text.lower()
    hits = {m.group(0) for m in _BAD_RE.finditer(t)}
    hits |= {m.group(0) for m in _BAD_RE.finditer(t.translate(_LEET))}
    return sorted(hits)


def is_clean(text: str) -> bool:
    return not bad_words(text)


def clean_title(title: str, max_len: int = 70) -> str:
    """Stream titles are full of emoji walls, !commands, links and ALL CAPS. Make a short readable line."""
    t = _EMOJI.sub(" ", title or "")
    t = re.sub(r"https?://\S+|\S+\.(com|tv|gg)\S*", " ", t)
    t = re.sub(r"[!@]\w+", " ", t)                 # !commands, @mentions
    t = re.sub(r"[\[\]{}|<>#*_~^]+", " ", t)
    t = re.sub(r"([?.\-])\1+", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—:;,/")
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        keep = {"GTA", "VI", "6", "NZ", "PS5", "PC", "RP", "IRL", "USA", "UK", "EST", "PST"}
        t = " ".join(w if w.upper() in keep or len(w) <= 2 and w.isupper() and w in keep else w.capitalize()
                     for w in t.split())
    if len(t) > max_len:
        t = t[:max_len].rsplit(" ", 1)[0].rstrip(" -–—:;,/") + "…"
    return t


def fmt_when(moment_ts: float, started_ts: float | None, tz: str) -> tuple[str, str]:
    z = ZoneInfo(tz)
    d = dt.datetime.fromtimestamp(moment_ts, z)
    when = d.strftime("%b %-d, %-I:%M %p ") + (d.tzname() or "")
    into = ""
    if started_ts and moment_ts > started_ts:
        mins = int((moment_ts - started_ts) // 60)
        h, m = divmod(mins, 60)
        into = f"{h}h {m:02d}m into the stream" if h else f"{m}m into the stream"
    return when.strip(), into


BASE_TAGS = ["#gta6", "#gtavi", "#gta", "#vicecity", "#leonida", "#gta6clips", "#twitchclips", "#twitch",
             "#streamer", "#gaming", "#gamingclips", "#viralclips", "#gta6gameplay", "#rockstargames"]


def pick_hashtags(streamer: str, account_tags: list[str], last: set[str] | None, rng: random.Random,
                  n: int = 6) -> list[str]:
    """Always #gta6 + the streamer; fill from account vibe tags + base pool; never the same set twice."""
    own = "#" + re.sub(r"[^a-z0-9_]", "", streamer.lower())
    for _ in range(10):
        pool = [t for t in dict.fromkeys(account_tags + BASE_TAGS) if t not in ("#gta6", own)]
        chosen = ["#gta6", own] + rng.sample(pool, k=min(n - 2, len(pool)))
        if not last or set(chosen) != last:
            return chosen
    return chosen


def build_caption(*, hook: str, streamer: dict, stream: dict, moment_ts: float, account: dict, tz: str,
                  last_tags: set[str] | None = None, rng: random.Random | None = None) -> dict:
    """-> {caption, hashtags, user_tags, dropped: [...reasons]}. Guaranteed clean or raises ValueError."""
    rng = rng or random.Random()
    dropped = []
    name = streamer["name"]
    display = stream.get("display") or name
    ig = (streamer.get("ig") or "").lstrip("@")
    if not is_clean(hook):
        dropped.append(f"hook:{bad_words(hook)}")
        hook = ""
    title = clean_title(stream.get("title", ""))
    if title and not is_clean(title):
        dropped.append(f"title:{bad_words(title)}")
        title = ""
    when, into = fmt_when(moment_ts, stream.get("started"), tz)
    who = f"@{ig}" if ig else display
    lines = []
    if hook:
        lines.append(hook)
        lines.append("")
    lines.append(f"🎮 {who} live on Twitch" + (f" — “{title}”" if title else ""))
    lines.append("🕒 " + when + (f" · {into}" if into else ""))
    lines.append(f"📺 twitch.tv/{name}")
    lines.append(f"🎬 Clipped by @{account.get('handle', '').lstrip('@')} · all credit to {who}")
    tags = pick_hashtags(name, account.get("hashtags", []), last_tags, rng)
    tags = [t for t in tags if is_clean(t)]
    lines.append("")
    lines.append(" ".join(tags))
    caption = "\n".join(lines)
    if not is_clean(caption):                     # belt and braces — should be impossible now
        raise ValueError(f"caption still not clean: {bad_words(caption)}")
    return {"caption": caption[:2200], "hashtags": tags, "user_tags": [ig] if ig else [], "dropped": dropped}
