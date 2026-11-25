"""Instagram Reels publisher — Instagram API with Instagram Login (graph.instagram.com, v27.0).

Instagram Login has no resumable upload, so Meta must fetch the video from a public URL:
we upload the finished MP4 to Cloudflare R2, hand Meta a presigned GET URL (2h), and delete the
object once the Reel is published (or failed).

Flow (docs read 2026-11-18): POST /{ig}/media (media_type=REELS, video_url, caption, share_to_feed,
thumb_offset, collaborators) -> poll GET /{container}?fields=status_code (FINISHED/ERROR/EXPIRED/
IN_PROGRESS/PUBLISHED) -> POST /{ig}/media_publish (creation_id) -> GET /{media}?fields=permalink.
Quota: GET /{ig}/content_publishing_limit?fields=quota_usage,config (docs: quota_total currently 50/24h).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import log

L = log.get("publisher")
GRAPH = "https://graph.instagram.com/v27.0"          # Instagram-Login tokens (IGAA...)
GRAPH_NOVER = "https://graph.instagram.com"
GRAPH_FB = "https://graph.facebook.com/v27.0"        # Unified/Facebook-Login tokens (EAA...)


def graph_for(token: str) -> str:
    return GRAPH_FB if token.startswith("EAA") else GRAPH


class PublishError(Exception):
    """kind: transient | auth | blocked | rate | limit | clip"""

    def __init__(self, kind: str, msg: str, code: int | None = None, subcode: int | None = None):
        super().__init__(f"[{kind}] {msg}")
        self.kind, self.msg, self.code, self.subcode = kind, msg, code, subcode

    @property
    def pauses_account(self) -> bool:
        return self.kind in ("auth", "blocked", "rate", "limit")


def classify(status: int, body: dict | None) -> PublishError:
    err = (body or {}).get("error") or {}
    code, sub = err.get("code"), err.get("error_subcode")
    msg = (err.get("error_user_msg") or err.get("message") or f"HTTP {status}")[:300]
    low = msg.lower()
    if status >= 500 or err.get("is_transient") or code in (1, 2):
        return PublishError("transient", msg, code, sub)
    if code == 190 or err.get("type") == "OAuthException" and code in (102, 463, 467):
        return PublishError("auth", msg, code, sub)
    if code == 9 or sub == 2207042 or "publishing limit" in low:
        return PublishError("limit", msg, code, sub)
    if code == 368 or "action blocked" in low or "spam" in low or "temporarily blocked" in low:
        return PublishError("blocked", msg, code, sub)
    if code in (4, 17, 32, 613):
        return PublishError("rate", msg, code, sub)
    if code == 10 or (code and 200 <= code < 300):
        return PublishError("auth", f"permission: {msg}", code, sub)
    return PublishError("clip", msg, code, sub)


# ---------------------------------------------------------------- R2 storage
class R2:
    def __init__(self):
        import boto3
        from botocore.config import Config

        acct = os.environ["R2_ACCOUNT_ID"]
        self.bucket = os.environ["R2_BUCKET"]
        self.s3 = boto3.client(
            "s3", endpoint_url=f"https://{acct}.r2.cloudflarestorage.com", region_name="auto",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    def put(self, path: Path) -> tuple[str, str]:
        key = f"reels/{uuid.uuid4().hex}.mp4"
        self.s3.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": "video/mp4"})
        url = self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                             ExpiresIn=7200)
        return key, url

    def delete(self, key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            L.warning("R2 delete %s failed: %s", key, e)

    def sweep(self, older_than_s: int = 86400) -> int:
        n = 0
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix="reels/")
        for o in resp.get("Contents", []):
            if time.time() - o["LastModified"].timestamp() > older_than_s:
                self.delete(o["Key"])
                n += 1
        return n


# ---------------------------------------------------------------- publisher
@dataclass
class Account:
    name: str           # config key, e.g. "acc1"
    handle: str
    ig_user_id: str
    token: str


@dataclass
class Published:
    media_id: str
    permalink: str
    container_id: str
    timestamp: str


class Publisher:
    def __init__(self, account: Account, storage=None, client: httpx.AsyncClient | None = None,
                 poll_s: float = 20, poll_timeout_s: float = 360):
        self.acc = account
        self.storage = storage
        self.http = client or httpx.AsyncClient(timeout=60)
        self.poll_s, self.poll_timeout_s = poll_s, poll_timeout_s
        self.g = graph_for(account.token)
        self.fb = self.g == GRAPH_FB

    def _redact(self, s: str) -> str:
        return s.replace(self.acc.token, "***") if self.acc.token else s

    async def _call(self, method: str, url: str, *, params=None, data=None, attempts: int = 4) -> dict:
        params = dict(params or {})
        data = dict(data) if data is not None else None
        if data is not None:
            data["access_token"] = self.acc.token
        else:
            params["access_token"] = self.acc.token
        delay = 2.0
        last: PublishError | None = None
        for i in range(attempts):
            try:
                r = await self.http.request(method, url, params=params, data=data)
            except httpx.HTTPError as e:      # network-level; message may contain URL -> redact
                last = PublishError("transient", self._redact(f"{type(e).__name__}: {e}"))
            else:
                try:
                    body = r.json()
                except ValueError:
                    body = None
                if r.status_code < 400 and not (isinstance(body, dict) and "error" in body):
                    return body or {}
                last = classify(r.status_code, body)
                last.msg = self._redact(last.msg)
                if last.kind != "transient":
                    raise last
            if i < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
        assert last
        raise last

    # -- reads
    async def whoami(self) -> dict:
        if self.fb:   # /me is the Facebook/system user here; read the IG account node instead
            r = await self._call("GET", f"{self.g}/{self.acc.ig_user_id}",
                                 params={"fields": "id,username,media_count,followers_count"})
            return {"user_id": r.get("id"), "username": r.get("username"), "media_count": r.get("media_count"),
                    "account_type": "IG_BUSINESS(fb-graph)", "followers": r.get("followers_count")}
        return await self._call("GET", f"{GRAPH}/me", params={"fields": "user_id,username,account_type,media_count"})

    async def quota(self) -> tuple[int, int]:
        r = await self._call("GET", f"{self.g}/{self.acc.ig_user_id}/content_publishing_limit",
                             params={"fields": "quota_usage,config"})
        d = (r.get("data") or [{}])[0]
        return int(d.get("quota_usage", 0)), int((d.get("config") or {}).get("quota_total", 50))

    async def recent_media(self, limit: int = 10) -> list[dict]:
        r = await self._call("GET", f"{self.g}/{self.acc.ig_user_id}/media",
                             params={"fields": "id,caption,timestamp,permalink,media_product_type", "limit": limit})
        return r.get("data", [])

    async def find_posted(self, caption: str) -> dict | None:
        """Idempotency: was this caption already published (e.g. crash between publish and DB write)?"""
        key = caption.strip()            # FULL caption: it contains the exact moment time, so different clips
        for m in await self.recent_media(15):   # of the same streamer/title never collide (prefix match did)
            if (m.get("caption") or "").strip() == key:
                return m
        return None

    async def container_status(self, container_id: str) -> str:
        r = await self._call("GET", f"{self.g}/{container_id}", params={"fields": "status_code,status"})
        return r.get("status_code", "UNKNOWN") + (f" ({r['status']})" if r.get("status") else "")

    # -- writes
    async def create_container(self, video_url: str, caption: str, thumb_offset_ms: int = 0,
                               collaborators: list[str] | None = None, share_to_feed: bool = True) -> str:
        data = {"media_type": "REELS", "video_url": video_url, "caption": caption,
                "share_to_feed": "true" if share_to_feed else "false", "thumb_offset": str(int(thumb_offset_ms))}
        if collaborators:
            data["collaborators"] = ",".join(collaborators[:3])
        r = await self._call("POST", f"{self.g}/{self.acc.ig_user_id}/media", data=data, attempts=2)
        return r["id"]

    async def wait_finished(self, container_id: str) -> None:
        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            st = await self.container_status(container_id)
            code = st.split(" ")[0]
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise PublishError("clip", f"container {code}: {st}")
            if time.monotonic() > deadline:
                raise PublishError("clip", f"container not ready after {self.poll_timeout_s}s ({st})")
            await asyncio.sleep(self.poll_s)

    async def publish_container(self, container_id: str) -> Published:
        r = await self._call("POST", f"{self.g}/{self.acc.ig_user_id}/media_publish",
                             data={"creation_id": container_id}, attempts=1)   # never blind-retry a publish
        mid = r["id"]
        info = await self._call("GET", f"{self.g}/{mid}", params={"fields": "permalink,timestamp"})
        return Published(mid, info.get("permalink", ""), container_id, info.get("timestamp", ""))

    async def _upload_resumable(self, video: Path, caption: str, thumb_offset_ms: int,
                                collaborators: list[str] | None) -> str:
        """Facebook-Login/Unified tokens: upload bytes straight to rupload.facebook.com (no hosting).
        rupload intermittently answers 400 ProcessingFailedError ("retriable": false) for a file that uploads
        fine minutes later (seen 2026-11-19) -> each retry uses a FRESH container, backoff 5s/20s/45s."""
        data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                "thumb_offset": str(int(thumb_offset_ms))}
        if collaborators:
            data["collaborators"] = ",".join(collaborators[:3])
        body = await asyncio.to_thread(video.read_bytes)
        err: PublishError | None = None
        for attempt, wait in enumerate([0, 5, 20, 45]):
            if wait:
                await asyncio.sleep(wait)
            r = await self._call("POST", f"{self.g}/{self.acc.ig_user_id}/media", data=data, attempts=2)
            cid, uri = r["id"], r["uri"]
            try:
                u = await self.http.post(uri, content=body, timeout=600, headers={
                    "Authorization": f"OAuth {self.acc.token}", "offset": "0", "file_size": str(len(body))})
                j = u.json() if u.content else {}
                if u.status_code < 400 and j.get("success"):
                    return cid
                dbg = (j.get("debug_info") or {}).get("type", "")
                err = classify(u.status_code, j)
                err.msg = self._redact(f"upload {u.status_code} {dbg} {err.msg}")
                if err.pauses_account:
                    raise err
            except httpx.HTTPError as e:
                err = PublishError("transient", self._redact(f"upload {type(e).__name__}: {e}"))
        assert err
        raise err

    async def prepare(self, video: Path, caption: str, thumb_offset_ms: int = 0,
                      collaborators: list[str] | None = None) -> tuple[str, str | None]:
        """Upload + create container (run ~5 min before slot). Returns (container_id, r2_key or None)."""
        if self.fb:
            return await self._upload_resumable(video, caption, thumb_offset_ms, collaborators), None
        assert self.storage, "R2 storage not configured"
        key, url = await asyncio.to_thread(self.storage.put, video)
        try:
            cid = await self.create_container(url, caption, thumb_offset_ms, collaborators)
        except Exception:
            await asyncio.to_thread(self.storage.delete, key)
            raise
        return cid, key

    async def finish(self, container_id: str, r2_key: str | None) -> Published:
        """Wait for processing, then publish. Deletes the R2 object either way."""
        try:
            await self.wait_finished(container_id)
            return await self.publish_container(container_id)
        finally:
            if r2_key and self.storage:
                await asyncio.to_thread(self.storage.delete, r2_key)

    async def refresh_token(self) -> tuple[str, int]:
        """Long-lived token refresh (token must be >=24h old). Returns (token, expires_in_s)."""
        r = await self._call("GET", f"{GRAPH_NOVER}/refresh_access_token", params={"grant_type": "ig_refresh_token"})
        return r["access_token"], int(r.get("expires_in", 0))


def accounts_from_env(cfg_accounts: list[dict]) -> list[Account]:
    out = []
    for i, a in enumerate(cfg_accounts, 1):
        n = a.get("env_index", i)
        uid, tok = os.environ.get(f"IG_USER_ID_{n}", ""), os.environ.get(f"IG_TOKEN_{n}", "")
        if uid and tok:
            out.append(Account(a["name"], a.get("handle", ""), uid, tok))
    return out
