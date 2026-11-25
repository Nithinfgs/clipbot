from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time

from .config import LOGS


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": round(r.created, 3), "lvl": r.levelname, "comp": r.name, "msg": r.getMessage()}
        if hasattr(r, "extra_data"):
            d.update(r.extra_data)
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, default=str, ensure_ascii=False)


def setup() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOGS / "clipbot.jsonl", maxBytes=50_000_000, backupCount=5)
    fh.setFormatter(JsonFormatter())
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s %(name)-10s %(levelname)-5s %(message)s", "%H:%M:%S"))
    sh.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.Formatter.converter = time.localtime
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def kv(logger: logging.Logger, level: int, msg: str, **data) -> None:
    logger.log(level, msg, extra={"extra_data": data})
