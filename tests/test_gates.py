from clipbot.config import load_config
from clipbot.gates import check, choose_account, overlaps, vibe_scores

ACCS = load_config()["accounts"]
OK_PROBE = {"duration": 25, "w": 1080, "h": 1920, "audio": True, "size": 30e6}


def test_gates_pass_and_fail():
    assert check({"speech_ratio": 0.4, "loud_ratio": 0.3}, OK_PROBE, {"black": 0, "static": 0}, True) == []
    bad = check({"speech_ratio": 0.0, "loud_ratio": 0.1}, {**OK_PROBE, "duration": 8}, {"black": 0.9, "static": 0.8}, False)
    assert len(bad) == 5


def test_vibes():
    assert max(vibe_scores(["kekw x30", "lol x10"]).items(), key=lambda x: x[1])[0] == "funny"
    assert max(vibe_scores(["huh x45", "?? x9"]).items(), key=lambda x: x[1])[0] == "reactions"
    assert max(vibe_scores(["cops x12", "chase x5"]).items(), key=lambda x: x[1])[0] == "chaos"


def test_assignment_prefers_vibe_and_respects_variety():
    v = vibe_scores(["kekw x30", "lol x20"])
    args = dict(clip_streamer="kaicenat", tier="B", score=5, vibes=v, accounts=ACCS,
                ready_count={a["name"]: 3 for a in ACCS}, paused=set(), now=10_000)
    assert choose_account(recent_posts={}, **args) == "acc1"
    # last post on acc1 was kaicenat -> never twice in a row
    assert choose_account(recent_posts={"acc1": [("kaicenat", 9_000)]}, **args) != "acc1"
    # paused account never chosen
    assert choose_account(recent_posts={}, **{**args, "paused": {"acc1"}}) != "acc1"


def test_best_of_gets_big_tier_a_moments():
    v = vibe_scores(["W x5"])
    a = choose_account(clip_streamer="xqc", tier="A", score=9, vibes=v, accounts=ACCS,
                       ready_count={x["name"]: 3 for x in ACCS}, recent_posts={}, paused=set(), now=0)
    assert a == "acc5"


def test_overlap():
    assert overlaps(100, 130, 125, 150) and not overlaps(100, 130, 200, 230)
