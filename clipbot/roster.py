"""Who we clip right now. Owner rule: only streams that are LIVE AND PLAYING GTA 6; when big names are
offline/off-game, pull in smaller live GTA 6 streamers so there is always enough to clip.

Game detection:
  * Twitch Helix API (official; needs TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET in .env) -> exact game,
    and discovery of every English stream in the "Grand Theft Auto VI" category sorted by viewers.
  * Without Helix creds: stream-title keywords (GTA 6 / GTA VI / Vice City / Leonida ...). No discovery.
"""
from __future__ import annotations

import os
import re
import time

import httpx

from . import log

L = log.get("roster")
GAME_NAME = "Grand Theft Auto VI"


def title_says_gta6(title: str, keywords: list[str]) -> bool:
    t = (title or "").lower()
    t = re.sub(r"\s+", " ", t)
    return any(k in t for k in keywords)


_GTA_WORD = re.compile(r"(?<![a-z0-9])gta(?!\s*(5|v\b|rp|online|iv\b|4|sa\b))(?![a-z0-9])")


def gta6_by_title(title: str, cfg: dict) -> bool:
    """No Helix creds: decide from the title. GTA 6 keywords or a bare 'GTA' -> yes; a different game named
    -> no; title says nothing about the game -> yes (every listed channel was picked from the GTA VI category)."""
    r = cfg.get("roster", {})
    t = re.sub(r"\s+", " ", (title or "").lower())
    if title_says_gta6(t, [k.lower() for k in r.get("gta6_title_keywords", [])]) or _GTA_WORD.search(t):
        return True
    if any(h in t + " " for h in r.get("other_game_hints", [])):
        return False
    return True


def blocked(login: str, cfg: dict) -> bool:
    r = cfg.get("roster", {})
    l = login.lower()
    return l in {b.lower() for b in r.get("blocklist", [])} or any(p in l for p in r.get("blocklist_patterns", []))


def tier_for(viewers: int, cfg: dict) -> str:
    tv = cfg.get("roster", {}).get("tier_viewers", {"A": 15000, "B": 5000})
    return "A" if viewers >= tv["A"] else "B" if viewers >= tv["B"] else "C"


class Helix:
    """Minimal official Twitch API client (app access token, client-credentials flow)."""

    def __init__(self, client_id: str, secret: str):
        self.cid, self.secret = client_id, secret
        self.token = ""
        self.token_exp = 0.0
        self.http = httpx.AsyncClient(timeout=20)
        self.game_id: str | None = None

    @classmethod
    def from_env(cls) -> "Helix | None":
        cid, sec = os.environ.get("TWITCH_CLIENT_ID", ""), os.environ.get("TWITCH_CLIENT_SECRET", "")
        return cls(cid, sec) if cid and sec else None

    async def _auth(self) -> None:
        if self.token and time.time() < self.token_exp - 300:
            return
        r = await self.http.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": self.cid, "client_secret": self.secret, "grant_type": "client_credentials"})
        r.raise_for_status()
        j = r.json()
        self.token, self.token_exp = j["access_token"], time.time() + j.get("expires_in", 3600)

    async def get(self, path: str, params) -> dict:
        await self._auth()
        r = await self.http.get(f"https://api.twitch.tv/helix/{path}", params=params,
                                headers={"Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
        if r.status_code == 401:
            self.token = ""
            await self._auth()
            r = await self.http.get(f"https://api.twitch.tv/helix/{path}", params=params,
                                    headers={"Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
        r.raise_for_status()
        return r.json()

    async def gta6_id(self) -> str:
        if not self.game_id:
            j = await self.get("games", {"name": GAME_NAME})
            self.game_id = j["data"][0]["id"]
        return self.game_id

    async def gta6_streams(self, language: str = "en", limit: int = 300) -> list[dict]:
        """Live streams in the GTA VI category, highest viewers first."""
        gid = await self.gta6_id()
        out, cursor = [], None
        while len(out) < limit:
            p = [("game_id", gid), ("language", language), ("first", "100"), ("type", "live")]
            if cursor:
                p.append(("after", cursor))
            j = await self.get("streams", p)
            out += j.get("data", [])
            cursor = (j.get("pagination") or {}).get("cursor")
            if not cursor or not j.get("data"):
                break
        return out

    async def streams_for(self, logins: list[str]) -> dict[str, dict]:
        res = {}
        for i in range(0, len(logins), 100):
            j = await self.get("streams", [("user_login", l) for l in logins[i:i + 100]])
            for s in j.get("data", []):
                res[s["user_login"].lower()] = s
        return res


def stream_from_helix(s: dict) -> dict:
    import datetime as dt
    started = dt.datetime.fromisoformat(s["started_at"].replace("Z", "+00:00")).timestamp()
    return {"display": s.get("user_name") or s["user_login"], "title": s.get("title", ""), "started": started,
            "viewers": s.get("viewer_count"), "game": s.get("game_name", "")}
