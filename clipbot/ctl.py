"""./ctl <command>
  check            verify .env: each IG account (token, account type, quota) + R2 upload/presign/fetch/delete
  pause [acc]      stop posting (all accounts, or one: acc1..acc5)
  resume [acc]     resume posting
  status           posts per account, ready clips, paused accounts
  refresh-tokens   refresh every long-lived IG token (needs token >= 24h old); rewrites .env, records expiry
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import sys
import tempfile
from pathlib import Path

import httpx

from .config import ROOT, load_env
from .publisher import GRAPH, Account, Publisher, PublishError, R2

ENV = ROOT / ".env"


def env_accounts() -> list[tuple[int, Account]]:
    out = []
    for n in range(1, 11):
        uid, tok = os.environ.get(f"IG_USER_ID_{n}", ""), os.environ.get(f"IG_TOKEN_{n}", "")
        if tok:
            out.append((n, Account(f"acc{n}", "", uid, tok)))
    return out


async def check() -> int:
    bad = 0
    accts = env_accounts()
    print(f"== Instagram accounts in .env: {len(accts)}")
    for n, a in accts:
        p = Publisher(a)
        try:
            me = await p.whoami()
            uid_ok = not a.ig_user_id or a.ig_user_id == str(me.get("user_id"))
            used, total = await p.quota() if a.ig_user_id else (None, None)
            exp = os.environ.get(f"IG_TOKEN_EXPIRES_{n}", "unknown")
            print(f"  [{n}] @{me.get('username')} type={me.get('account_type')} media={me.get('media_count')} "
                  f"user_id={me.get('user_id')}{'' if uid_ok else '  !! IG_USER_ID mismatch'} "
                  f"quota={used}/{total} token_expires={exp}")
            if not a.ig_user_id:
                print(f"      -> add IG_USER_ID_{n}={me.get('user_id')} to .env")
                bad += 1
            bad += not uid_ok
        except PublishError as e:
            bad += 1
            print(f"  [{n}] FAIL {e}")
    print("== Cloudflare R2")
    if accts and all(a.token.startswith("EAA") for _, a in accts):
        print("  not needed: all accounts use Unified/Facebook-login tokens -> direct resumable upload")
    else:
        bad += await _check_r2()
    for k in ("ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        print(f"== {k}: {'set' if os.environ.get(k) else 'MISSING'}")
    return 1 if bad else 0


async def _check_r2() -> int:
    bad = 0
    try:
        r2 = R2()
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"clipbot r2 check")
        key, url = await asyncio.to_thread(r2.put, Path(f.name))
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url)
        ok = r.status_code == 200 and r.content == b"clipbot r2 check"
        await asyncio.to_thread(r2.delete, key)
        print(f"  upload+presigned public fetch: {'OK' if ok else f'FAIL HTTP {r.status_code}'}")
        bad += not ok
    except KeyError as e:
        bad += 1
        print(f"  missing {e} in .env")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"  FAIL {type(e).__name__}: {str(e)[:200]}")
    return bad


def _set_env(lines: list[str], key: str, val: str) -> list[str]:
    pat = re.compile(rf"^{re.escape(key)}=")
    for i, l in enumerate(lines):
        if pat.match(l):
            lines[i] = f"{key}={val}"
            return lines
    return lines + [f"{key}={val}"]


async def refresh_tokens() -> int:
    lines = ENV.read_text().splitlines()
    bad = 0
    for n, a in env_accounts():
        try:
            tok, exp_s = await Publisher(a).refresh_token()
            exp = (dt.datetime.now() + dt.timedelta(seconds=exp_s)).strftime("%Y-%m-%d")
            lines = _set_env(lines, f"IG_TOKEN_{n}", tok)
            lines = _set_env(lines, f"IG_TOKEN_EXPIRES_{n}", exp)
            print(f"  [{n}] refreshed, expires {exp}")
        except PublishError as e:
            bad += 1
            print(f"  [{n}] not refreshed: {e}")
    ENV.write_text("\n".join(lines) + "\n")
    return 1 if bad else 0


def main() -> None:
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "check":
        sys.exit(asyncio.run(check()))
    if cmd in ("pause", "resume"):
        from .config import DATA
        f = DATA / ("PAUSE" + (f"_{sys.argv[2]}" if len(sys.argv) > 2 else ""))
        if cmd == "pause":
            f.write_text("paused"); print("paused", f.name)
        else:
            f.unlink(missing_ok=True); print("resumed", f.name)
        sys.exit(0)
    if cmd == "status":
        from . import db
        con = db.connect()
        for acc, st, n in con.execute("SELECT account, status, count(*) FROM clips WHERE account IS NOT NULL "
                                      "GROUP BY account, status ORDER BY account"):
            print(f"{acc:6} {st:12} {n}")
        for r in con.execute("SELECT account, permalink, datetime(posted_at,'unixepoch','localtime') FROM clips "
                             "WHERE status='posted' ORDER BY posted_at DESC LIMIT 10"):
            print("posted", *r)
        sys.exit(0)
    if cmd == "refresh-tokens":
        sys.exit(asyncio.run(refresh_tokens()))
    print(__doc__)


if __name__ == "__main__":
    _ = GRAPH
    main()
