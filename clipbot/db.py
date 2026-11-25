from __future__ import annotations

import json
import sqlite3
import time

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
  id INTEGER PRIMARY KEY, streamer TEXT, tier TEXT, peak_ts REAL, score REAL,
  breakdown TEXT, top_messages TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY, trigger_id INTEGER, streamer TEXT, platform TEXT, tier TEXT,
  peak_ts REAL, start_ts REAL, end_ts REAL, source TEXT, status TEXT,
  raw_path TEXT, error TEXT, meta TEXT, account TEXT, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS clips_streamer ON clips(streamer, start_ts);
CREATE TABLE IF NOT EXISTS health (
  component TEXT PRIMARY KEY, ok INTEGER, detail TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, level TEXT, component TEXT, msg TEXT
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def insert(con: sqlite3.Connection, table: str, **row) -> int:
    row = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
    cols = ",".join(row)
    q = ",".join("?" * len(row))
    return con.execute(f"INSERT INTO {table} ({cols}) VALUES ({q})", list(row.values())).lastrowid


def update_clip(con: sqlite3.Connection, clip_id: int, **fields) -> None:
    fields["updated_at"] = time.time()
    fields = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
    sets = ",".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE clips SET {sets} WHERE id=?", [*fields.values(), clip_id])


def set_health(con: sqlite3.Connection, component: str, ok: bool, detail: str = "") -> None:
    con.execute(
        "INSERT INTO health VALUES (?,?,?,?) ON CONFLICT(component) DO UPDATE SET ok=excluded.ok, detail=excluded.detail, updated_at=excluded.updated_at",
        (component, int(ok), detail, time.time()),
    )
