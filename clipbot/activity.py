"""Community requirement: a CSV log of everything that's been done.
logs/activity.csv — one row per event, appended immediately (opens fine in Excel/Numbers/Sheets).

events: trigger, clip_ready, clip_failed, edited, rejected, assigned, posted, post_failed,
        account_paused, account_resumed, deleted_old_clip, run_start, run_stop
"""
from __future__ import annotations

import csv
import datetime as dt
import threading
from pathlib import Path

from .config import LOGS

PATH = LOGS / "activity.csv"
COLUMNS = ["time_local", "event", "clip_id", "streamer", "stream_title", "moment_time", "account",
           "permalink", "score", "duration_s", "caption_preview", "detail"]
_lock = threading.Lock()


def record(event: str, path: Path | None = None, **fields) -> None:
    path = path or PATH
    row = {c: "" for c in COLUMNS}
    row["time_local"] = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    row["event"] = event
    for k, v in fields.items():
        if k in row and v is not None:
            row[k] = str(v).replace("\n", " ⏎ ")[:300]
    with _lock:
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerow(row)
