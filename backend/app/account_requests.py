"""Account requests: a stranger asks, an operator decides.

Registration is invite-gated because ``/auth/register`` cannot answer "does this
address have an account?" without becoming an enumeration oracle — see
``app/invites.py``. A request endpoint has exactly the same problem, and the same
answer: it always says 202, and the decision happens in the panel, where the
caller is an authenticated operator rather than a stranger.

That makes the operator the gate. A request is not an account: nothing here
touches ``dogwalk:users:*``. Approving one does, and that lives in
``admin.approve_request`` so the JSON API and the HTML page share it.

Keys:

* ``dogwalk:account_requests``           - sorted set: request_id -> created_at
* ``dogwalk:account_requests:<id>``      - hash: email, created_at
* ``dogwalk:account_requests:email:<e>`` - email -> request_id, the dedupe index

The dedupe index is what stops one address filling the queue, and it is written
with ``SET NX`` so two simultaneous requests for the same address cannot both
win. The cap bounds what an unauthenticated endpoint can put in Redis; the rate
limiter in ``app/ratelimit.py`` bounds how fast one client can get there.
"""

import time
import uuid

from app import users

INDEX_KEY = "dogwalk:account_requests"
REQUEST_KEY = "dogwalk:account_requests:{request_id}"
EMAIL_KEY = "dogwalk:account_requests:email:{email}"

# A backlog, not a queue: past this the operator is not keeping up, and a
# request that is dropped is one the requester can simply send again.
MAX_PENDING = 100


async def create(redis, email: str) -> str | None:
    """Record a request. Returns its id, or ``None`` when it was suppressed.

    Suppressed means the address already has an account, already asked, or the
    backlog is full. The caller answers 202 either way: telling a stranger which
    of those happened would say whether the address is already registered.
    """
    email = users.normalise_email(email)

    # Nothing to decide for an address that is already in. Checking here rather
    # than in the route keeps the operator's queue free of requests that could
    # only ever be answered with a 409.
    if await users.get_by_email(redis, email) is not None:
        return None

    request_id = uuid.uuid4().hex

    # NX first, so the dedupe index is the thing that decides. A read-then-write
    # would let two requests for the same address both pass the check.
    if not await redis.set(EMAIL_KEY.format(email=email), request_id, nx=True):
        return None

    if await count(redis) >= MAX_PENDING:
        await redis.delete(EMAIL_KEY.format(email=email))
        return None

    now = time.time()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(
            REQUEST_KEY.format(request_id=request_id),
            mapping={"email": email, "created_at": int(now)},
        )
        pipe.zadd(INDEX_KEY, {request_id: now})
        await pipe.execute()
    return request_id


async def get(redis, request_id: str) -> dict | None:
    record = await redis.hgetall(REQUEST_KEY.format(request_id=request_id))
    if not record:
        return None
    return {"request_id": request_id, **record}


async def pending(redis, limit: int = MAX_PENDING) -> list[dict]:
    """Newest first, from the index rather than a keyspace scan."""
    ids = await redis.zrevrange(INDEX_KEY, 0, max(limit - 1, 0))
    if not ids:
        return []

    async with redis.pipeline(transaction=False) as pipe:
        for request_id in ids:
            pipe.hgetall(REQUEST_KEY.format(request_id=request_id))
        records = await pipe.execute()

    return [
        {"request_id": request_id, **record}
        for request_id, record in zip(ids, records)
        if record
    ]


async def count(redis) -> int:
    return int(await redis.zcard(INDEX_KEY))


async def remove(redis, request_id: str) -> dict | None:
    """Drop a request and its dedupe entry. Returns what was removed, or None.

    The record comes back so the caller can audit the address: after this the
    only copy of it is in the audit entry.
    """
    record = await get(redis, request_id)
    if record is None:
        return None

    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(REQUEST_KEY.format(request_id=request_id))
        pipe.delete(EMAIL_KEY.format(email=record["email"]))
        pipe.zrem(INDEX_KEY, request_id)
        await pipe.execute()
    return record
