import csv
import os
import time

from clipbot import activity
from clipbot.storage import enforce


def _mk(root, name, size, age_s):
    p = root / name
    p.write_bytes(b"x" * size)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_deletes_oldest_first_until_under_cap(tmp_path):
    old = _mk(tmp_path, "old.mp4", 400, 300)
    mid = _mk(tmp_path, "mid.mp4", 400, 200)
    new = _mk(tmp_path, "new.mp4", 400, 100)
    deleted = enforce(max_gb=900 / 1e9, min_free_gb=0, root=tmp_path, _free=lambda r: 999)
    assert deleted == [old, mid] or deleted == [old]
    assert new.exists() and not old.exists()


def test_low_disk_triggers_cleanup_and_respects_protected(tmp_path):
    old = _mk(tmp_path, "old.ts", 100, 300)
    mid = _mk(tmp_path, "mid.ts", 100, 200)
    new = _mk(tmp_path, "new.ts", 100, 100)
    deleted = enforce(max_gb=100, min_free_gb=10, protected={str(old)}, root=tmp_path,
                      _free=lambda r: 10 - 50e-9)   # 50 bytes short of 10 GB free
    assert old.exists()                          # protected (being posted)
    assert deleted[0] == mid and new.exists()


def test_activity_csv_appends_rows(tmp_path):
    p = tmp_path / "a.csv"
    activity.record("trigger", path=p, clip_id=1, streamer="xqc", score=6.5, detail="lol x58")
    activity.record("posted", path=p, clip_id=1, account="vicecity.laughs", permalink="https://instagram.com/reel/x",
                    caption_preview="line1\nline2")
    rows = list(csv.DictReader(p.open()))
    assert [r["event"] for r in rows] == ["trigger", "posted"]
    assert rows[1]["permalink"].endswith("/reel/x") and "\n" not in rows[1]["caption_preview"]
