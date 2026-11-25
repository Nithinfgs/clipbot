import random

from clipbot.config import load_config
from clipbot.detector import Detector

CFG = load_config()
T0 = 1_800_000_000.0


def make(seed=1):
    random.seed(seed)
    d = Detector(CFG)
    d.add_channel("ch", "A", T0)
    return d


def baseline(d, start, secs, rate=3, pool=300):
    """Normal chatter: ~rate msgs/sec from a pool of users, mostly non-hype."""
    trig = []
    words = ["gg", "what map is this", "hello chat", "he should go left", "nice car", "lol", "true", "chat is he cooking"]
    for s in range(secs):
        t = start + s
        for _ in range(random.randint(rate - 1, rate + 1)):
            d.on_message("ch", f"u{random.randint(0, pool)}", random.choice(words), t + random.random())
        trig += d.tick(t + 1)
    return trig


def burst(d, start, secs, per_sec, text_fn, user_fn):
    trig = []
    for s in range(secs):
        t = start + s
        for i in range(per_sec):
            d.on_message("ch", user_fn(s, i), text_fn(s, i), t + random.random())
        trig += d.tick(t + 1)
    return trig


def test_quiet_chat_no_trigger():
    d = make()
    assert baseline(d, T0, 900) == []


def test_real_hype_triggers_once_with_peak():
    d = make()
    assert baseline(d, T0, 600) == []
    t = T0 + 600
    trig = burst(d, t, 6, 35, lambda s, i: random.choice(["KEKW", "LMAOOOO", "NO WAY", "💀💀💀", "OMEGALUL"]),
                 lambda s, i: f"h{s}_{i}")
    trig += baseline(d, t + 6, 120)
    assert len(trig) == 1, trig
    assert t <= trig[0].peak_ts <= t + 7
    assert trig[0].breakdown["unique_z"] > 3


def test_warmup_blocks():
    d = make()
    trig = baseline(d, T0, 60) + burst(d, T0 + 60, 6, 35, lambda s, i: "KEKW", lambda s, i: f"h{s}_{i}")
    assert trig == []


def test_raid_does_not_trigger():
    d = make()
    baseline(d, T0, 600)
    t = T0 + 600
    d.on_message("ch", "raider", "", t, kind="usernotice", notice_id="raid", tags={"msg-param-viewerCount": "5000"})
    trig = burst(d, t, 20, 40, lambda s, i: random.choice(["raid hype", "W streamer", "hi from raid KEKW"]),
                 lambda s, i: f"r{s}_{i}")
    trig += baseline(d, t + 20, 30)
    assert trig == []


def test_gift_bomb_does_not_trigger():
    d = make()
    baseline(d, T0, 600)
    t = T0 + 600
    d.on_message("ch", "gifter", "", t, kind="usernotice", notice_id="submysterygift",
                 tags={"msg-param-mass-gift-count": "100"})
    for i in range(100):  # individual subgift notices
        d.on_message("ch", "gifter", "", t + i * 0.05, kind="usernotice", notice_id="subgift")
    trig = burst(d, t, 15, 30, lambda s, i: random.choice(["W GIFTER", "ty for the sub", "W"]), lambda s, i: f"g{s}_{i}")
    assert trig == []


def test_copypasta_does_not_trigger():
    d = make()
    baseline(d, T0, 600)
    pasta = "this is the longest copypasta in the history of chat i swear to god it never ends"
    trig = burst(d, T0 + 600, 15, 40, lambda s, i: pasta, lambda s, i: f"p{s}_{i}")
    trig += baseline(d, T0 + 615, 60)
    assert trig == []


def test_bot_spam_does_not_trigger():
    d = make()
    baseline(d, T0, 600)
    trig = burst(d, T0 + 600, 20, 30, lambda s, i: f"!command reply number {s}-{i} KEKW",
                 lambda s, i: random.choice(["nightbot", "streamelements", "fossabot"]))
    trig += burst(d, T0 + 620, 20, 15, lambda s, i: f"spam {s} {i}", lambda s, i: "onespammer")
    assert trig == []


def test_cooldown():
    d = make()
    baseline(d, T0, 600)
    hype = lambda s, i: "KEKW"  # noqa: E731
    trig = burst(d, T0 + 600, 6, 35, hype, lambda s, i: f"a{s}_{i}")
    trig += baseline(d, T0 + 606, 10)
    trig += burst(d, T0 + 616, 6, 35, hype, lambda s, i: f"b{s}_{i}")  # inside 75s cooldown
    assert len(trig) == 1


def test_norm_strips_invisible_bypass_chars():
    from clipbot.detector import norm_text
    assert norm_text("lol ͏") == norm_text("LOL") == "lol"
    assert norm_text("KEKW\U000e0000") == "kekw"


def test_reconnect_burst_does_not_trigger():
    d = make()
    baseline(d, T0, 600)
    t = T0 + 600
    d.set_connected("ch", False, t)
    for s in range(120):                      # 2 min outage: no messages, ticks continue
        d.tick(t + s + 1)
    t2 = t + 120
    d.set_connected("ch", True, t2)
    # chat returns at normal-but-busy volume (looks like a spike vs a decayed baseline)
    trig = burst(d, t2, 30, 8, lambda s, i: random.choice(["lol", "gg", "what", "KEKW"]), lambda s, i: f"x{s}_{i}")
    assert trig == []
    st = d.channels["ch"]
    assert st.rate.mean > 1.5                 # baseline was frozen, not decayed to ~0


def test_vote_flood_does_not_trigger():
    d = make()
    baseline(d, T0, 600)
    trig = burst(d, T0 + 600, 15, 20, lambda s, i: random.choice(["viper", "viper", "viper", "banshee"]),
                 lambda s, i: f"v{s}_{i}")
    trig += baseline(d, T0 + 615, 60)
    assert trig == []


def test_hype_word_flood_still_triggers():
    d = make()
    baseline(d, T0, 600)
    trig = burst(d, T0 + 600, 6, 35, lambda s, i: "LMAO", lambda s, i: f"h{s}_{i}")
    assert len(trig + baseline(d, T0 + 606, 30)) == 1


def test_type_me_in_chat_prompt_does_not_trigger():
    d = make()
    baseline(d, T0, 600)
    trig = burst(d, T0 + 600, 12, 30, lambda s, i: random.choice(["me", "me", "mee", "meee", "me", "me", "ME", "yoo"]),
                 lambda s, i: f"m{s}_{i}")
    trig += baseline(d, T0 + 612, 60)
    assert trig == []


def test_lexicon_survives_normalisation():
    d = make()
    for t in ["???", "LMAOOOO", "KEKW", "no wayyyy", "💀💀💀", "OMEGALUL"]:
        assert d.is_hype(__import__("clipbot.detector", fromlist=["norm_text"]).norm_text(t)), t
