"""Anonymous Twitch IRC over websocket. One connection handles many channels."""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Callable

import websockets

from .. import log

L = log.get("chat.tw")
URL = "wss://irc-ws.chat.twitch.tv:443"


@dataclass
class ChatMsg:
    channel: str
    user: str
    text: str
    ts: float
    kind: str = "msg"            # msg | usernotice
    notice_id: str = ""          # sub, resub, subgift, submysterygift, raid, ...
    tags: dict = field(default_factory=dict)


def _unescape(v: str) -> str:
    return v.replace(r"\s", " ").replace(r"\:", ";").replace(r"\\", "\\").replace(r"\r", "").replace(r"\n", "")


def parse(line: str) -> tuple[dict, str, str, list[str]]:
    tags: dict = {}
    prefix = ""
    if line.startswith("@"):
        raw, line = line[1:].split(" ", 1)
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape(v)
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
    trailing = None
    if " :" in line:
        line, trailing = line.split(" :", 1)
    parts = line.split()
    cmd, params = (parts[0] if parts else ""), parts[1:]
    if trailing is not None:
        params.append(trailing)
    return tags, prefix, cmd, params


class TwitchChat:
    """Joins channels; calls on_msg(ChatMsg). Reconnects forever with backoff."""

    def __init__(self, channels: list[str], on_msg: Callable[[ChatMsg], None], on_state: Callable[[str, bool], None]):
        self.channels = set(c.lower() for c in channels)
        self.on_msg = on_msg
        self.on_state = on_state
        self.ws = None
        self.last_rx: dict[str, float] = {}
        self.connected = False

    async def join(self, ch: str) -> None:
        ch = ch.lower()
        self.channels.add(ch)
        if self.ws and self.connected:
            await self.ws.send(f"JOIN #{ch}")

    async def part(self, ch: str) -> None:
        ch = ch.lower()
        self.channels.discard(ch)
        if self.ws and self.connected:
            await self.ws.send(f"PART #{ch}")

    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                async with websockets.connect(URL, ping_interval=30, ping_timeout=60, max_size=2**22, max_queue=4096) as ws:
                    self.ws = ws
                    await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                    await ws.send("PASS SCHMOOPIIE")
                    await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
                    chans = sorted(self.channels)
                    for i in range(0, len(chans), 10):      # stay under JOIN rate limit
                        await ws.send("JOIN " + ",".join(f"#{c}" for c in chans[i:i + 10]))
                        await asyncio.sleep(1.2)
                    self.connected = True
                    backoff = 1
                    L.info("connected, joined %d channels", len(chans))
                    async for frame in ws:
                        for line in frame.split("\r\n"):
                            if line:
                                self._handle(ws, line)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                L.warning("disconnected: %s; reconnect in %ss", e, backoff)
            self.connected = False
            for c in self.channels:
                self.on_state(c, False)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _handle(self, ws, line: str) -> None:
        if line.startswith("PING"):
            asyncio.ensure_future(ws.send("PONG :tmi.twitch.tv"))
            return
        tags, prefix, cmd, params = parse(line)
        if cmd not in ("PRIVMSG", "USERNOTICE", "JOIN", "ROOMSTATE"):
            return
        ch = params[0].lstrip("#") if params else ""
        if cmd in ("JOIN", "ROOMSTATE"):
            self.on_state(ch, True)
            return
        now = time.time()
        self.last_rx[ch] = now
        ts = int(tags.get("tmi-sent-ts", 0)) / 1000 or now
        user = tags.get("login") or tags.get("display-name", "").lower() or prefix.split("!")[0]
        text = params[1] if len(params) > 1 else ""
        if cmd == "USERNOTICE":
            self.on_msg(ChatMsg(ch, user, text, ts, "usernotice", tags.get("msg-id", ""), tags))
        else:
            self.on_msg(ChatMsg(ch, user, text, ts, "msg", "", tags))
