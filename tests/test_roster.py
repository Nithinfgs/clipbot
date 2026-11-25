from clipbot.config import load_config
from clipbot.roster import blocked, gta6_by_title, tier_for, title_says_gta6

CFG = load_config()
KW = CFG["roster"]["gta6_title_keywords"]


def test_title_detects_gta6_variants():
    for t in ["🌴PLAYING GTA 6 FOR THE FIRST TIME🌴", "[DROPS] GTA VI EARLY ACCESS", "gta6 marathon", "Vice City tonight",
              "LEONIDA 100%", "Grand Theft Auto VI blind run"]:
        assert title_says_gta6(t, KW), t


def test_title_rejects_other_games():
    for t in ["Just chatting w/ chat", "Valorant ranked grind", "GTA 5 RP NoPixel", "Fortnite with the boys", ""]:
        assert not title_says_gta6(t, KW), t


def test_blocklist_and_tiers():
    assert blocked("sashagrey", CFG) and blocked("badgecollectors", CFG) and blocked("twisterdrops", CFG)
    assert not blocked("kaicenat", CFG)
    assert (tier_for(20000, CFG), tier_for(6000, CFG), tier_for(900, CFG)) == ("A", "B", "C")


def test_gta6_by_title_fallback():
    yes = ["🚨 FIRST PERSON TO PLAY GTA 🚨 BILLY JONES", "(DROPS ON) GTA LAUNCH DAY w/ GIRLFRIEND",
           "Cutscene% Speedrun! Giveaways! [DROPS]", "I have never been more excited for a game", ""]
    no = ["Just chatting w/ chat", "Valorant ranked grind", "GTA 5 RP NoPixel", "back to GTA V tonight",
          "Fortnite with the boys", "GTA Online heists"]
    assert all(gta6_by_title(t, CFG) for t in yes), [t for t in yes if not gta6_by_title(t, CFG)]
    assert not any(gta6_by_title(t, CFG) for t in no), [t for t in no if gta6_by_title(t, CFG)]
