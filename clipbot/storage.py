"""Clip storage — community rule: keep clips anywhere on the laptop; when storage fills, delete the
OLDEST clips first so the newest are kept.

"Full" = clips/ exceeds clips_max_gb OR the disk has less than min_free_gb free.
Files of clips currently being posted/scheduled are never deleted (passed in as `protected`).
The rolling stream buffer (buffer/) is managed by the recorder, not here.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import log
from .config import CLIPS

L = log.get("storage")
EXTS = {".ts", ".mp4", ".mov", ".jpg", ".png", ".ass", ".json", ".wav"}


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in EXTS]


def usage_gb(root: Path = CLIPS) -> float:
    return sum(p.stat().st_size for p in _files(root)) / 1e9


def free_gb(path: Path = CLIPS) -> float:
    return shutil.disk_usage(path).free / 1e9


def enforce(max_gb: float, min_free_gb: float, protected: set[str] | None = None, root: Path = CLIPS,
            on_delete=None, _free=free_gb) -> list[Path]:
    """Delete oldest-first until under max_gb and above min_free_gb. Returns deleted paths."""
    protected = {str(Path(p).resolve()) for p in (protected or set())}
    files = sorted(_files(root), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    deleted: list[Path] = []
    freed = 0
    for p in files:
        over = total / 1e9 > max_gb
        low = _free(root) + freed / 1e9 < min_free_gb
        if not (over or low):
            break
        if str(p.resolve()) in protected:
            continue
        size = p.stat().st_size
        try:
            p.unlink()
        except FileNotFoundError:
            continue
        total -= size
        freed += size
        deleted.append(p)
        if on_delete:
            on_delete(p, size)
    if deleted:
        L.info("storage: deleted %d oldest clip files (%.2f GB) — clips now %.2f GB, disk free %.1f GB",
               len(deleted), freed / 1e9, total / 1e9, _free(root))
    return deleted
