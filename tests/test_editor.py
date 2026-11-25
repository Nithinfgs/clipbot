from clipbot.editor import build_ass, group_words, hook_from_chat, smart_trim
import numpy as np


def _events(ass, style):
    out = []
    for l in ass.splitlines():
        if l.startswith("Dialogue:") and f",{style}," in l:
            p = l.split(",", 9)
            def t(x):
                h, m, s = x.split(":"); return int(h) * 3600 + int(m) * 60 + float(s)
            out.append((t(p[1]), t(p[2]), p[9]))
    return out


W = [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.25} for i, w in enumerate("bro what the hell is that shit no way man".split())]


def test_captions_never_overlap_and_swears_masked():
    ass = build_ass(W, "NO WAY 😱", "acc", "&H0000D7FF&", 10, 1300, 400, 1500)
    ev = sorted(_events(ass, "Cap"))
    assert all(a[1] <= b[0] + 1e-6 for a, b in zip(ev, ev[1:]))
    assert "SHIT" not in ass and "S***" in ass


def test_hook_has_no_emoji_on_screen():
    hook = _events(build_ass(W, "NO WAY THIS HAPPENED 😱", "acc", "&H0000D7FF&", 10, 1300, 400, 1500), "Hook")[0][2]
    assert "😱" not in hook and "NO WAY THIS HAPPENED" in hook


def test_groups_max_four_words():
    assert all(1 <= len(g) <= 4 for g in group_words(W))


def test_hook_from_chat_picks_reaction():
    assert "😂" in hook_from_chat(["kekw x30", "lol x20"], "Kai")
    assert "😳" in hook_from_chat(["huh x45", "?? x9"], "xQc")
    h = hook_from_chat([], "Lacy")
    assert h and "fuck" not in h.lower()


def test_smart_trim_keeps_peak_and_bounds():
    env = np.full(300, 0.01); env[150:220] = 0.3        # loud from 15s to 22s
    a, b = smart_trim(env, [], 30.0, 22.0)
    assert a <= 12.0 and b >= 25.0 and 12 <= b - a <= 40
