"""Per-IP throttling for the auth endpoints.

The per-email lockout in ``users.py`` stops one account being brute forced. It
does nothing about credential stuffing across many accounts, or about
``/auth/register`` being hammered to fill Redis. This is the other half: a
fixed-window counter per client address and route.

Fixed windows are coarse — a burst can straddle a boundary and get two windows'
worth — but they are one INCR and one EXPIRE, which is the right cost for a
control that exists to blunt automated abuse rather than to meter traffic.

The client address comes from ``X-Forwarded-For``, which Caddy overwrites with
the real peer address (``header_up X-Forwarded-For {remote_host}``). The backend
port is bound to loopback in ``docker-compose.yml`` so that header cannot be
spoofed by reaching the app directly.
"""

import time
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse

KEY = "dogwalk:ratelimit:{bucket}:{identity}"


@dataclass(frozen=True)
class Limit:
    requests: int
    window_seconds: int


# Keyed by exact path. Anything not listed is not throttled.
LIMITS: dict[str, Limit] = {
    "/auth/login": Limit(10, 60),
    # Generous enough that re-running scripts/e2e.py on a dev machine does not
    # trip it, tight enough that filling Redis with accounts is not free.
    "/auth/register": Limit(20, 3600),
    "/auth/refresh": Limit(30, 60),
    "/auth/logout": Limit(30, 60),
    "/auth/logout-all": Limit(10, 60),
    "/auth/password-reset/request": Limit(3, 3600),
    "/auth/password-reset/confirm": Limit(10, 3600),
    "/auth/account": Limit(5, 3600),
    # Admin accounts can delete every account in the deployment, so the login is
    # throttled like a user login rather than left open. The HTML form and the
    # JSON endpoint are separate paths and each gets its own bucket.
    "/admin/auth/login": Limit(10, 60),
    "/admin/ui/login": Limit(10, 60),
}

# Keyed by path prefix, for routes whose last segment is an id. The bucket is
# the prefix rather than the full path: bucketing on the id would hand every id
# its own counter, which is the same as no limit at all.
PREFIX_LIMITS: dict[str, Limit] = {
    # Blocking writes two Redis keys per call and accepts any 32-hex id, so the
    # ceiling in ``blocks.MAX_BLOCKS`` bounds one account and this bounds how
    # fast an account can get there.
    "/blocks/": Limit(60, 60),
}


def match(path: str) -> tuple[str, Limit] | None:
    """The bucket and limit for ``path``, or ``None`` when it is not throttled.

    Exact paths win over prefixes so a specific route can be tightened without
    being shadowed by a broader rule.
    """
    limit = LIMITS.get(path)
    if limit is not None:
        return path, limit

    for prefix, prefix_limit in PREFIX_LIMITS.items():
        if path.startswith(prefix):
            return prefix, prefix_limit
    return None


def client_identity(request: Request) -> str:
    """The address to bill this request to.

    Caddy sets ``X-Forwarded-For`` to the real peer, so it is preferred. Falling
    back to the socket address keeps the limiter working when the app is run
    without the proxy, as the tests do.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client is not None else "unknown"


async def check(redis, identity: str, path: str, limit: Limit) -> int:
    """Count this request. Returns 0 when allowed, else seconds until reset."""
    window = int(time.time()) // limit.window_seconds
    key = KEY.format(bucket=f"{path}:{window}", identity=identity)

    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, limit.window_seconds)

    if count > limit.requests:
        ttl = await redis.ttl(key)
        return max(int(ttl), 1)
    return 0


def install(app) -> None:
    """Register the middleware on ``app``."""

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        matched = match(request.url.path)
        if matched is None or request.method == "OPTIONS":
            return await call_next(request)
        bucket, limit = matched

        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            return await call_next(request)

        retry_after = await check(redis, client_identity(request), bucket, limit)
        if retry_after:
            return JSONResponse(
                status_code=429,
                content={"detail": "too many requests"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
