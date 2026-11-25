from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BUFFER = ROOT / "buffer"
CLIPS = ROOT / "clips"
LOGS = ROOT / "logs"
DATA = ROOT / "data"
DB_PATH = DATA / "clipbot.db"


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0]           # allow trailing "   # comment"
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    cfg = yaml.safe_load(path.read_text())
    for s in cfg["streamers"]:
        s["name"] = s["name"].lower()
        s.setdefault("capture", "auto")
        if s["capture"] == "auto":
            s["capture"] = "live" if s["tier"] == "A" else "vod"
    return cfg


for d in (BUFFER, CLIPS, LOGS, DATA):
    d.mkdir(exist_ok=True)
