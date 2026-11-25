"""Quality gates + account assignment (pure functions, unit-tested).

Gates (all must pass to post; owner: video content itself may be anything):
  duration 12-60s · 1080x1920 · audio present · not junk screen (black / static loading screen)
  · not dead air (speech or loud audio) · file size · description clean (captions.py) · no duplicate moment
"""
from __future__ import annotations

import re

VIBES = {
    "funny": r"\b(kekw|lul|lol|lmao|omegalul|icant|haha|xd|😂|🤣|lul)\b",
    "fails": r"\b(rip|l|f|dead|wasted|unlucky|sadge|bruh|💀|nooo|noo|no)\b",
    "reactions": r"(\?\?|\bhuh\b|\bwhat\b|\bomg\b|\bno ?way\b|😳|😱|\bholy\b|\bwow\b)",
    "chaos": r"\b(cops?|police|stars?|chase|wanted|crash|explod\w*|boom|run|swat|tank|heli)\b",
}


def vibe_scores(top_messages: list[str]) -> dict[str, float]:
    parts = []
    for m in top_messages:
        txt, _, n = m.rpartition(" x")
        if not txt or not n.isdigit():
            txt, n = m, "1"
        parts.append((txt + " ") * max(1, min(int(n), 20)))
    text = " ".join(parts).lower()
    return {v: float(len(re.findall(p, text))) for v, p in VIBES.items()}


def check(edit: dict, probe: dict, frames: dict, caption_clean: bool) -> list[str]:
    """-> list of failure reasons ([] = pass)."""
    bad = []
    d = probe.get("duration", 0)
    if not 12 <= d <= 60:
        bad.append(f"duration {d:.1f}s")
    if (probe.get("w"), probe.get("h")) != (1080, 1920):
        bad.append(f"size {probe.get('w')}x{probe.get('h')}")
    if not probe.get("audio"):
        bad.append("no audio")
    if probe.get("size", 0) > 280e6:
        bad.append("file too big")
    if frames.get("black", 0) > 0.4:
        bad.append(f"black screen {frames['black']:.0%}")
    if frames.get("static", 0) > 0.5:
        bad.append(f"static/loading screen {frames['static']:.0%}")
    if edit.get("speech_ratio", 0) < 0.08 and edit.get("loud_ratio", 0) < 0.25:
        bad.append("dead air")
    if not caption_clean:
        bad.append("description not clean")
    return bad


def overlaps(a0: float, a1: float, b0: float, b1: float, pad: float = 5.0) -> bool:
    return a0 < b1 + pad and b0 < a1 + pad


def choose_account(*, clip_streamer: str, tier: str, score: float, vibes: dict[str, float],
                   accounts: list[dict], ready_count: dict[str, int], recent_posts: dict[str, list[tuple]],
                   paused: set[str], now: float) -> str | None:
    """Exclusive assignment. recent_posts[acc] = [(streamer, ts), ...] newest last (posted or ready).
    Rules: never same streamer twice in a row on an account; max 3 per streamer per 2h per account."""
    best, best_s = None, -1e9
    for a in accounts:
        name = a["name"]
        if name in paused:
            continue
        hist = recent_posts.get(name, [])
        if hist and hist[-1][0] == clip_streamer:
            continue
        if sum(1 for s, t in hist if s == clip_streamer and now - t < 7200) >= 3:
            continue
        s = 0.0
        v = a.get("vibe")
        if v == "best_of":
            s += 2.5 if tier == "A" and score >= 6 else (1.0 if tier == "A" else -1.0)
        else:
            tot = sum(vibes.values()) or 1
            s += 3.0 * vibes.get(v, 0) / tot
        s += 1.2 * max(0, 3 - ready_count.get(name, 0))        # fill accounts whose buffer is low
        s -= 0.6 * max(0, ready_count.get(name, 0) - 4)
        if s > best_s:
            best, best_s = name, s
    return best
