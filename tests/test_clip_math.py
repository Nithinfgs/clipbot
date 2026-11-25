from clipbot.clipper import window
from clipbot.config import load_config
from clipbot.vodfetch import parse_playlist

PL = """#EXTM3U
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T12:59:53.334Z
#EXTINF:10.000,
0.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T13:00:03.334Z
#EXTINF:10.000,
1-muted.ts
"""


def test_parse_playlist():
    segs = parse_playlist(PL)
    assert [s[2] for s in segs] == ["0.ts", "1-muted.ts"]
    assert segs[1][0] - segs[0][0] == 10.0
    assert segs[0][1] == 10.0


def test_window():
    cfg = load_config()
    a, b = window(1000.0, cfg, "live")
    assert (a, b) == (1000 - cfg["clip"]["pre_s"], 1000 + cfg["clip"]["post_s"])
    cfg["clip"]["vod_latency_s"] = 7
    a2, b2 = window(1000.0, cfg, "vod")
    assert (a2, b2) == (a - 7, b - 7)


def test_parse_playlist_without_pdt_matches_pdt():
    with_pdt = PL.replace("#EXT-X-PLAYLIST-TYPE:EVENT", "#ID3-EQUIV-TDTG:2026-11-18T13:00:13\n#EXT-X-TWITCH-TOTAL-SECS:20.000")
    no_pdt = "\n".join(l for l in with_pdt.splitlines() if "PROGRAM-DATE" not in l) + "\n"
    a, b = parse_playlist(with_pdt), parse_playlist(no_pdt)
    assert [s[2] for s in a] == [s[2] for s in b]
    assert abs(a[0][0] - b[0][0]) < 1.0


FMP4 = """#EXTM3U
#EXT-X-VERSION:6
#ID3-EQUIV-TDTG:2026-11-18T13:00:20
#EXT-X-TWITCH-TOTAL-SECS:20.000
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
0.mp4
#EXTINF:10.000,
1.mp4
"""


def test_parse_fmp4_tracks_init_map():
    segs = parse_playlist(FMP4)
    assert [s[2] for s in segs] == ["0.mp4", "1.mp4"]
    assert all(s[3] == "init-0.mp4" for s in segs)
    assert segs[1][0] - segs[0][0] == 10.0


def test_parse_ts_has_no_map():
    assert all(s[3] is None for s in parse_playlist(PL))
