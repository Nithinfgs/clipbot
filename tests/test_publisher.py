import asyncio
from pathlib import Path

import httpx
import pytest

from clipbot.publisher import Account, Publisher, PublishError, classify

TOKEN = "IGAAsecret_token_value_123"


class FakeR2:
    def __init__(self):
        self.objects, self.deleted = set(), []

    def put(self, path):
        self.objects.add("reels/x.mp4")
        return "reels/x.mp4", "https://r2.example/reels/x.mp4?sig=abc"

    def delete(self, key):
        self.deleted.append(key)


def make(handler, storage=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Publisher(Account("acc1", "h", "1789", TOKEN), storage, client, poll_s=0, poll_timeout_s=5)


def run(coro):
    return asyncio.run(coro)


def test_happy_path(tmp_path: Path):
    calls = []
    statuses = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])

    def h(req: httpx.Request):
        calls.append((req.method, req.url.path))
        body = req.content.decode()
        if req.method == "POST" and req.url.path.endswith("/1789/media"):
            assert "media_type=REELS" in body and "share_to_feed=true" in body and "thumb_offset=4200" in body
            assert "video_url=https%3A%2F%2Fr2.example" in body
            return httpx.Response(200, json={"id": "C1"})
        if req.url.path.endswith("/C1"):
            return httpx.Response(200, json={"status_code": next(statuses)})
        if req.url.path.endswith("/media_publish"):
            assert "creation_id=C1" in body
            return httpx.Response(200, json={"id": "M1"})
        if req.url.path.endswith("/M1"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/", "timestamp": "t"})
        raise AssertionError(req.url)

    r2 = FakeR2()
    p = make(h, r2)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    cid, key = run(p.prepare(f, "caption #gta6", 4200))
    res = run(p.finish(cid, key))
    assert res.permalink.endswith("/reel/abc/") and res.media_id == "M1"
    assert r2.deleted == ["reels/x.mp4"]


def test_container_error_is_clip_failure_and_cleans_r2():
    def h(req):
        return httpx.Response(200, json={"status_code": "ERROR", "status": "2207026"})

    r2 = FakeR2()
    p = make(h, r2)
    with pytest.raises(PublishError) as e:
        run(p.finish("C9", "reels/x.mp4"))
    assert e.value.kind == "clip" and not e.value.pauses_account
    assert r2.deleted == ["reels/x.mp4"]


@pytest.mark.parametrize("status,err,kind", [
    (400, {"code": 190, "message": "Error validating access token"}, "auth"),
    (400, {"code": 10, "message": "Application does not have permission"}, "auth"),
    (400, {"code": 368, "message": "The action attempted has been deemed abusive"}, "blocked"),
    (400, {"code": 4, "message": "Application request limit reached"}, "rate"),
    (400, {"code": 9, "error_subcode": 2207042, "message": "reached publishing limit"}, "limit"),
    (500, {"code": 2, "message": "Service temporarily unavailable"}, "transient"),
    (400, {"code": 100, "error_subcode": 2207026, "message": "unsupported video format"}, "clip"),
])
def test_classify(status, err, kind):
    assert classify(status, {"error": err}).kind == kind


def test_transient_retries_then_succeeds():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] < 3:
            return httpx.Response(503, json={"error": {"code": 2, "message": "busy"}})
        return httpx.Response(200, json={"data": [{"quota_usage": 7, "config": {"quota_total": 50}}]})

    p = make(h)
    import clipbot.publisher as pub
    orig = asyncio.sleep
    pub.asyncio.sleep = lambda s: orig(0)
    try:
        assert run(p.quota()) == (7, 50)
    finally:
        pub.asyncio.sleep = orig
    assert n["c"] == 3


def test_publish_never_blind_retried():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(500, json={"error": {"code": 2, "message": "unknown"}})

    with pytest.raises(PublishError):
        run(make(h).publish_container("C1"))
    assert n["c"] == 1


def test_token_never_in_error_text():
    def h(req):
        raise httpx.ConnectError(f"failed connecting to {req.url}")

    import clipbot.publisher as pub
    orig = asyncio.sleep
    pub.asyncio.sleep = lambda s: orig(0)
    try:
        with pytest.raises(PublishError) as e:
            run(make(h).quota())
    finally:
        pub.asyncio.sleep = orig
    assert TOKEN not in str(e.value) and TOKEN not in e.value.msg


def test_find_posted_reconciles_by_caption():
    def h(req):
        return httpx.Response(200, json={"data": [
            {"id": "M5", "caption": "other", "permalink": "p5"},
            {"id": "M6", "caption": "HE DID NOT EXPECT THAT 💀 #gta6", "permalink": "p6"}]})

    m = run(make(h).find_posted("HE DID NOT EXPECT THAT 💀 #gta6"))
    assert m["id"] == "M6"


def test_fb_token_uses_resumable_upload_no_r2(tmp_path):
    seen = []

    def h(req):
        seen.append((req.method, req.url.host, req.url.path))
        if req.url.host == "graph.facebook.com" and req.url.path.endswith("/1789/media"):
            body = req.content.decode()
            assert "upload_type=resumable" in body and "media_type=REELS" in body and "video_url" not in body
            return httpx.Response(200, json={"id": "C7", "uri": "https://rupload.facebook.com/ig-api-upload/v27.0/C7"})
        if req.url.host == "rupload.facebook.com":
            assert req.headers["offset"] == "0" and int(req.headers["file_size"]) == 3
            assert req.headers["authorization"].startswith("OAuth ")
            return httpx.Response(200, json={"success": True, "message": "Upload Successful."})
        raise AssertionError(req.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    p = Publisher(Account("acc1", "h", "1789", "EAAfaketoken"), None, client, poll_s=0)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"abc")
    cid, key = run(p.prepare(f, "cap", 1500))
    assert (cid, key) == ("C7", None)
    assert any(h == "rupload.facebook.com" for _, h, _ in seen)


def test_find_posted_needs_full_caption_match():
    base = "CHAT COULDN'T BREATHE 😂\n\n🎮 Distortion2 live on Twitch — “1 Sub = Badge/PS5 PRO GTA VI IS HERE - Surely its insane”\n"

    def h(req):
        return httpx.Response(200, json={"data": [{"id": "M1", "caption": base + "🕒 Nov 19, 1:31 AM IST", "permalink": "p"}]})

    assert run(make(h).find_posted(base + "🕒 Nov 19, 4:34 AM IST")) is None
    assert run(make(h).find_posted(base + "🕒 Nov 19, 1:31 AM IST"))["id"] == "M1"
