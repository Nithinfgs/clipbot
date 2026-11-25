import random

import pytest

from clipbot.captions import bad_words, build_caption, clean_title, is_clean, pick_hashtags

T = 1795015000.0
ACC = {"handle": "vicecity.laughs", "hashtags": ["#funny", "#lol", "#gtafunny"]}


def cap(**kw):
    args = dict(hook="HE DID NOT EXPECT THAT 💀", streamer={"name": "kaicenat"},
                stream={"display": "KaiCenat", "title": "🌴PLAYING GTA 6 FOR THE FIRST TIME🌴", "started": T - 8040},
                moment_ts=T, account=ACC, tz="Asia/Kolkata", rng=random.Random(1))
    args.update(kw)
    return build_caption(**args)


def test_caption_has_creator_stream_time_link():
    c = cap()["caption"]
    assert "KaiCenat live on Twitch" in c
    assert "“Playing GTA 6 For The First Time”" in c
    assert "2h 14m into the stream" in c and "IST" in c
    assert "twitch.tv/kaicenat" in c
    assert "#gta6" in c and "#kaicenat" in c


def test_tags_streamer_only_when_handle_verified():
    assert cap()["user_tags"] == []
    r = cap(streamer={"name": "kaicenat", "ig": "@kaicenat"})
    assert r["user_tags"] == ["kaicenat"] and "@kaicenat live on Twitch" in r["caption"]


@pytest.mark.parametrize("t", ["what the fuck", "WTF bro", "sh1t", "f*ck", "sexo", "LMFAO", "b!tch", "porn", "18+ stream"])
def test_bad_words_detected(t):
    assert not is_clean(t.replace("*", "u")), bad_words(t)


@pytest.mark.parametrize("t", ["HE DID NOT EXPECT THAT", "class assignment", "Essex", "cocktail bar", "pass the ball",
                               "Dickens", "therapist", "Scunthorpe", "assassin", "hello"])
def test_no_false_positives(t):
    assert is_clean(t), bad_words(t)


def test_vulgar_hook_and_title_are_dropped_not_posted():
    r = cap(hook="WTF IS THIS 💀", stream={"display": "X", "title": "sexo stream !gamba", "started": None})
    assert "WTF" not in r["caption"] and "sexo" not in r["caption"]
    assert len(r["dropped"]) == 2 and is_clean(r["caption"])


def test_clean_title():
    assert clean_title("🔴 GTA 6 EARLY ACCESS 🔴 FIRST TIME EVER 🔴 !CLIPPING !GTA6") == "GTA 6 Early Access First Time Ever"
    assert "http" not in clean_title("gta6 now https://x.com/abc")
    assert len(clean_title("word " * 40)) <= 71


def test_hashtags_rotate_never_identical_back_to_back():
    rng = random.Random(3)
    a = pick_hashtags("xqc", ACC["hashtags"], None, rng)
    b = pick_hashtags("xqc", ACC["hashtags"], set(a), rng)
    assert set(a) != set(b) and 3 <= len(b) <= 6 and b[0] == "#gta6" and "#xqc" in b


@pytest.mark.parametrize("t", ["cumulative score", "dickhead", "cocks"])
def test_stem_edges(t):
    assert is_clean(t) == (t == "cumulative score")
