"""Post one finished Reel to one account (owner-approved manual post).
usage: uv run python tools/post_one.py <account_name> <video.mp4> <caption.txt> [thumb_ms]"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from clipbot import activity  # noqa: E402
from clipbot.config import load_config, load_env  # noqa: E402
from clipbot.publisher import accounts_from_env, Publisher  # noqa: E402


async def main(acc_name, video, cap_file, thumb_ms):
    load_env()
    cfg = load_config()
    acc = next(a for a in accounts_from_env(cfg["accounts"]) if a.name == acc_name)
    handle = next(a["handle"] for a in cfg["accounts"] if a["name"] == acc_name)
    caption = Path(cap_file).read_text(encoding="utf-8")
    p = Publisher(acc)
    used, total = await p.quota()
    print(f"@{handle}: quota {used}/{total}")
    if await p.find_posted(caption):
        print("already posted (same caption found) - not posting twice"); return
    cid, _ = await p.prepare(Path(video), caption, thumb_ms)
    print("container", cid, "uploaded; waiting for processing...")
    res = await p.finish(cid, None)
    print("PUBLISHED", res.permalink, res.media_id)
    activity.record("posted", clip_id=Path(video).stem.split("_")[0], account=handle, permalink=res.permalink,
                    caption_preview=caption[:120], detail=f"manual first post, media {res.media_id}")


asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 0))
