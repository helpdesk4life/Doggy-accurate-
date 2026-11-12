"""Shared helper: talk to the backend from a script.

The scripts register a throwaway account on first use and log in on later runs,
so re-running them does not accumulate accounts. Passwords are fixed and
obviously non-production; these accounts only exist on a dev backend.

Registration is invite-gated, so a script that needs a new account mints itself a
code straight into Redis — the same thing an operator does with
``scripts/mint_invites.py``. That keeps the scripts working without weakening the
gate.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# Running as `python scripts/e2e.py` puts scripts/ on sys.path, not the backend
# root, so `app` is not importable until the parent is added.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import invites  # noqa: E402

DEFAULT_PASSWORD = "script-walker-password"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def http_base(ws_url: str) -> str:
    """ws://host:port/ws/walk -> http://host:port"""
    base = ws_url.split("/ws/")[0]
    if base.startswith("wss://"):
        return "https://" + base[len("wss://"):]
    if base.startswith("ws://"):
        return "http://" + base[len("ws://"):]
    return base


def call(
    ws_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    """One HTTP call against the backend, with an optional bearer token."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        http_base(ws_url) + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def mint_invite() -> str:
    """Mint one code into Redis, the way an operator would.

    Uses the synchronous client on purpose: e2e.py calls this from inside a
    running event loop, where ``asyncio.run`` would raise.
    """
    import redis

    url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        while True:
            code = invites.generate()
            if client.sadd(invites.UNUSED_KEY, code):
                return invites.format_code(code)
    finally:
        client.close()


def reset_rate_limits() -> int:
    """Clear the per-IP buckets. Development harness only.

    The auth endpoints are throttled per hour, so a second e2e run in the same
    hour would fail on limits the first run spent rather than on anything real.
    The script sets up its own preconditions the same way it mints its own
    invite codes.

    Refuses to run against anything but a loopback backend, so pointing this at
    a shared deployment cannot wipe its limiter state.
    """
    import redis

    url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
    host = url.split("//", 1)[-1].split(":", 1)[0].split("/", 1)[0]
    if host not in {"localhost", "127.0.0.1", "redis", "::1"}:
        raise SystemExit(f"refusing to reset rate limits on non-local Redis at {host}")

    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        keys = list(client.scan_iter("dogwalk:ratelimit:*"))
        return int(client.delete(*keys)) if keys else 0
    finally:
        client.close()


def access_token(ws_url: str, email: str, password: str = DEFAULT_PASSWORD) -> str:
    """Register the account if it is new, otherwise log in. Returns an access token."""
    status, body = call(ws_url, "POST", "/auth/register", {"email": email, "password": password})
    if status == 403:
        # Invite-gated backend. Mint a code and try once more.
        status, body = call(
            ws_url,
            "POST",
            "/auth/register",
            {"email": email, "password": password, "invite_code": mint_invite()},
        )
    # 409: the account already exists. 429: the per-IP register limit is spent,
    # which on a dev machine usually means the account is already there too.
    if status in (409, 429):
        status, body = call(ws_url, "POST", "/auth/login", {"email": email, "password": password})
    if status not in (200, 201):
        raise SystemExit(f"auth failed for {email}: HTTP {status} {body}")
    return body["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
