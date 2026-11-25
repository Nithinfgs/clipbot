"""Render finished Reels from raw clips in the DB (manual test tool).
usage: uv run python tools/render_clip.py <clip_id> <account_name> [<clip_id> <account_name> ...]"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipbot import db  # noqa: E402
from clipbot.captions import build_caption  # noqa: E402
from clipbot.config import CLIPS, load_config  # noqa: E402
from clipbot.editor import edit, frame_stats, hook_from_chat, probe  # noqa: E402

cfg = load_config()
con = db.connect()
accounts = {a["name"]: a for a in cfg["accounts"]}
streamers = {s["name"]: s for s in cfg["streamers"]}
args = sys.argv[1:]
for cid, acc_name in zip(args[::2], args[1::2]):
    row = con.execute("SELECT c.*, t.top_messages FROM clips c LEFT JOIN triggers t ON t.id=c.trigger_id WHERE c.id=?",
                      (int(cid),)).fetchone()
    meta = json.loads(row["meta"] or "{}")
    stream = meta.get("stream", {})
    acc = accounts[acc_name]
    top = json.loads(row["top_messages"] or "[]")
    display = stream.get("display") or row["streamer"]
    hook = hook_from_chat(top, display, seed=int(cid))
    peak_rel = cfg["clip"]["pre_s"]          # window is always [peak-pre, peak+post] in its own timeline
    out = CLIPS / "final" / f"{cid}_{acc['handle']}.mp4"
    r = edit(Path(row["raw_path"]), meta.get("offset", 0), meta.get("duration", 30), peak_rel,
             acc["handle"], acc["color"], hook, out, CLIPS / "tmp")
    cap = build_caption(hook=hook, streamer=streamers.get(row["streamer"], {"name": row["streamer"]}), stream=stream,
                        moment_ts=row["peak_ts"], account=acc, tz=cfg["run"]["tz"], rng=random.Random(int(cid)))
    p = probe(out)
    fs = frame_stats(out)
    (out.with_suffix(".txt")).write_text(cap["caption"], encoding="utf-8")
    print(json.dumps({"clip": cid, "streamer": row["streamer"], "out": str(out), "dur": r.duration,
                      "took_s": r.took_s, "words": r.words, "speech": r.speech_ratio, "loud": r.loud_ratio,
                      "thumb_ms": r.thumb_offset_ms, "hook": hook, "probe": p, "frames": fs,
                      "transcript": r.transcript[:160]}, ensure_ascii=False))
