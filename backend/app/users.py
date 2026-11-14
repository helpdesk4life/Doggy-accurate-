"""Redis-backed user accounts.

Accounts live in Redis alongside the geo index rather than in a separate
database, which keeps the stack to one stateful service. ``docker-compose.yml``
gives Redis a volume and AOF so accounts survive a restart.

Keys:

* ``dogwalk:users:<user_id>``        - hash: email, password_hash, created_at, token_version
* ``dogwalk:users:email:<email>``    - email -> user_id, the uniqueness index
* ``dogwalk:users:<user_id>:refresh``- set of live refresh token ids, so an account
                                       can be deleted without scanning the keyspace
* ``dogwalk:refresh:<jti>``          - live refresh token ids, with a TTL
* ``dogwalk:refresh:used:<jti>``     - ids already exchanged, kept so a replay can be
                                       told apart from a token that never existed
* ``dogwalk:reset:<digest>``         - password reset codes, keyed by a hash of
                                       email + code so the code is never stored
* ``dogwalk:reset:attempts:<email>`` - wrong-code counter, bounds guessing
* ``dogwalk:loginfail:<email>``      - failed login counter, for lockout
"""

import hashlib
import secrets
import time
import uuid

from app import auth, blocks

USER_KEY = "dogwalk:users:{user_id}"
EMAIL_KEY = "dogwalk:users:email:{email}"
REFRESH_INDEX_KEY = "dogwalk:users:{user_id}:refresh"
# Sorted set of user_id by created_at, so the admin list can page newest-first
# without scanning the keyspace. Redis has no "list every hash" primitive that
# is safe to call on a live store, and SCAN cannot give a stable page.
CREATED_INDEX_KEY = "dogwalk:users:created"
REFRESH_KEY = "dogwalk:refresh:{jti}"
USED_REFRESH_KEY = "dogwalk:refresh:used:{jti}"
RESET_KEY = "dogwalk:reset:{digest}"
# The reset key is a digest of email+code, so it cannot be found from the address
# alone. This set is what makes the codes deletable: without it, deleting an
# account leaves every code it was ever sent alive until its TTL runs out.
RESET_INDEX_KEY = "dogwalk:reset:index:{email}"
RESET_ATTEMPT_KEY = "dogwalk:reset:attempts:{email}"
LOGIN_FAIL_KEY = "dogwalk:loginfail:{email}"

MAX_LOGIN_FAILURES = 10
LOGIN_FAILURE_WINDOW_SECONDS = 900

# How long a consumed refresh id is remembered. Long enough to catch a stolen
# token being replayed days later; short enough not to grow without bound.
USED_REFRESH_TTL_SECONDS = 86_400

# A client that loses the response to a successful refresh will retry with the
# same token. Treating that as theft would sign people out on a flaky train
# connection, so a replay inside this window is answered with a plain 401 and
# only a later one revokes the account's sessions.
REPLAY_GRACE_SECONDS = 60

RESET_TTL_SECONDS = 900
RESET_MAX_ATTEMPTS = 10

# Verified against when the address has no account, so that "no such user" and
# "wrong password" cost the same amount of time. Computed once at import.
_DUMMY_PASSWORD_HASH = auth.hash_password("dogwalk-dummy-password-not-a-real-account")


class EmailTaken(Exception):
    """Registration for an email that already has an account."""


class InvalidCredentials(Exception):
    """Wrong email or password. Deliberately does not say which."""


class LockedOut(Exception):
    """Too many failed logins for this email; try again later."""


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def create_user(
    redis, email: str, password: str, must_change_password: bool = False
) -> dict:
    """Create an account.

    ``must_change_password`` marks a password that somebody else chose — an
    operator setting the account up. The flag is what makes the first sign-in
    a password change rather than a session; see ``routes_auth.login``.
    """
    email = normalise_email(email)
    if await redis.exists(EMAIL_KEY.format(email=email)):
        raise EmailTaken(email)

    user_id = uuid.uuid4().hex
    now = time.time()
    record = {
        "email": email,
        "password_hash": auth.hash_password(password),
        "created_at": int(now),
        "token_version": 1,
    }
    if must_change_password:
        record["must_change_password"] = "1"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(USER_KEY.format(user_id=user_id), mapping=record)
        pipe.set(EMAIL_KEY.format(email=email), user_id)
        # Float score, so two accounts created in the same second still order
        # newest-first in the admin list. created_at stays an int for display.
        pipe.zadd(CREATED_INDEX_KEY, {user_id: now})
        await pipe.execute()

    return {"user_id": user_id, **record}


def must_change_password(user: dict) -> bool:
    """True while the account still has the password an operator handed over."""
    return user.get("must_change_password") == "1"


async def get_by_id(redis, user_id: str) -> dict | None:
    record = await redis.hgetall(USER_KEY.format(user_id=user_id))
    if not record:
        return None
    return {"user_id": user_id, **record}


async def get_by_email(redis, email: str) -> dict | None:
    user_id = await redis.get(EMAIL_KEY.format(email=normalise_email(email)))
    if user_id is None:
        return None
    return await get_by_id(redis, user_id)


async def list_users(redis, offset: int = 0, limit: int = 50) -> list[dict]:
    """Newest first, from the created index.

    One ``ZREVRANGE`` for the page and one pipeline for the hashes, so the cost
    is the page size rather than the number of accounts.
    """
    ids = await redis.zrevrange(CREATED_INDEX_KEY, offset, offset + limit - 1)
    if not ids:
        return []

    async with redis.pipeline(transaction=False) as pipe:
        for user_id in ids:
            pipe.hgetall(USER_KEY.format(user_id=user_id))
        records = await pipe.execute()

    return [
        {"user_id": user_id, **record}
        for user_id, record in zip(ids, records)
        if record
    ]


async def count_users(redis) -> int:
    return int(await redis.zcard(CREATED_INDEX_KEY))


async def session_counts(redis, user_ids: list[str]) -> dict[str, int]:
    """Live refresh tokens per account: how many devices each is signed in on."""
    if not user_ids:
        return {}
    async with redis.pipeline(transaction=False) as pipe:
        for user_id in user_ids:
            pipe.scard(REFRESH_INDEX_KEY.format(user_id=user_id))
        counts = await pipe.execute()
    return {user_id: int(count) for user_id, count in zip(user_ids, counts)}


async def authenticate(redis, email: str, password: str) -> dict:
    """Verify credentials, counting failures so an email cannot be brute forced."""
    email = normalise_email(email)
    fail_key = LOGIN_FAIL_KEY.format(email=email)

    failures = await redis.get(fail_key)
    if failures is not None and int(failures) >= MAX_LOGIN_FAILURES:
        raise LockedOut(email)

    user = await get_by_email(redis, email)

    # Always run the hash, even when the address is unknown. Returning early
    # here would make "no such account" measurably faster than "wrong password",
    # which is an account enumeration oracle.
    stored = user["password_hash"] if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = auth.verify_password(password, stored)

    if user is None or not password_ok:
        count = await redis.incr(fail_key)
        if count == 1:
            await redis.expire(fail_key, LOGIN_FAILURE_WINDOW_SECONDS)
        raise InvalidCredentials(email)

    await redis.delete(fail_key)
    return user


async def set_password(redis, user_id: str, password: str) -> None:
    """Replace the password and clear the handed-over flag.

    Both callers — the reset flow and the forced change — are the account
    choosing its own password, which is exactly what the flag is waiting for.
    Clearing it here rather than at each call site means a reset is also a way
    out of the forced-change flow, which is the escape hatch if the operator
    never passes the temporary password on.
    """
    key = USER_KEY.format(user_id=user_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, "password_hash", auth.hash_password(password))
        pipe.hdel(key, "must_change_password")
        await pipe.execute()


async def delete_user(redis, user_id: str) -> None:
    """Remove the account and everything hanging off it.

    The refresh index exists so this does not have to scan the keyspace for
    ``dogwalk:refresh:*``. The geo entry is left to the WebSocket handler, which
    removes it when the socket closes.

    Block lists go too, in both directions: the deleted account's own list, and
    the entries in other people's lists that point at it. Leaving those behind
    would show a name in someone's settings for an account that no longer exists.
    """
    user = await get_by_id(redis, user_id)
    if user is None:
        return

    await blocks.forget(redis, user_id)

    jtis = await redis.smembers(REFRESH_INDEX_KEY.format(user_id=user_id))
    # Any reset code still in flight dies with the account. Without this the code
    # outlives the account it belongs to, and confirming it writes a password
    # hash for a user that no longer exists.
    digests = await redis.smembers(RESET_INDEX_KEY.format(email=user["email"]))
    async with redis.pipeline(transaction=True) as pipe:
        for jti in jtis:
            if isinstance(jti, bytes):
                jti = jti.decode()
            pipe.delete(REFRESH_KEY.format(jti=jti))
        for digest in digests:
            if isinstance(digest, bytes):
                digest = digest.decode()
            pipe.delete(RESET_KEY.format(digest=digest))
        pipe.delete(REFRESH_INDEX_KEY.format(user_id=user_id))
        pipe.delete(RESET_INDEX_KEY.format(email=user["email"]))
        pipe.delete(USER_KEY.format(user_id=user_id))
        pipe.delete(EMAIL_KEY.format(email=user["email"]))
        pipe.delete(LOGIN_FAIL_KEY.format(email=user["email"]))
        pipe.delete(RESET_ATTEMPT_KEY.format(email=user["email"]))
        pipe.zrem(CREATED_INDEX_KEY, user_id)
        await pipe.execute()


async def store_refresh(redis, jti: str, user_id: str, ttl_seconds: int) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(REFRESH_KEY.format(jti=jti), user_id, ex=ttl_seconds)
        pipe.sadd(REFRESH_INDEX_KEY.format(user_id=user_id), jti)
        pipe.expire(REFRESH_INDEX_KEY.format(user_id=user_id), ttl_seconds)
        await pipe.execute()


async def consume_refresh(redis, jti: str) -> str | None:
    """Read and burn in one step: a refresh token is single use.

    The burned id is remembered for a day so a replay can be told apart from a
    token that never existed. See ``replay_of_consumed_refresh``.
    """
    key = REFRESH_KEY.format(jti=jti)
    user_id = await redis.get(key)
    if user_id is None:
        return None

    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(key)
        pipe.srem(REFRESH_INDEX_KEY.format(user_id=user_id), jti)
        pipe.set(
            USED_REFRESH_KEY.format(jti=jti),
            f"{user_id}|{int(time.time())}",
            ex=USED_REFRESH_TTL_SECONDS,
        )
        await pipe.execute()
    return user_id


async def replay_of_consumed_refresh(redis, jti: str) -> tuple[str, int] | None:
    """``(user_id, seconds_since_consumption)`` when this id was already exchanged."""
    raw = await redis.get(USED_REFRESH_KEY.format(jti=jti))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()

    user_id, _, consumed_at = raw.partition("|")
    try:
        age = int(time.time()) - int(consumed_at)
    except ValueError:
        return user_id, 0
    return user_id, age


async def revoke_refresh(redis, jti: str) -> None:
    key = REFRESH_KEY.format(jti=jti)
    user_id = await redis.get(key)
    if user_id is None:
        return
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(key)
        pipe.srem(REFRESH_INDEX_KEY.format(user_id=user_id), jti)
        await pipe.execute()


async def bump_token_version(redis, user_id: str) -> int:
    """Invalidate every token this user holds, on every device."""
    return await redis.hincrby(USER_KEY.format(user_id=user_id), "token_version", 1)


def _reset_digest(email: str, code: str) -> str:
    return hashlib.sha256(f"{normalise_email(email)}:{code}".encode()).hexdigest()


async def create_reset_code(redis, email: str) -> str:
    """Mint a six-digit reset code for an address. Returns the plaintext code.

    Only the hash is stored, so a Redis dump does not hand out working codes.
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    digest = _reset_digest(email, code)
    key = RESET_KEY.format(digest=digest)
    index = RESET_INDEX_KEY.format(email=normalise_email(email))
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, mapping={"email": normalise_email(email)})
        pipe.expire(key, RESET_TTL_SECONDS)
        pipe.sadd(index, digest)
        pipe.expire(index, RESET_TTL_SECONDS)
        await pipe.execute()
    return code


async def consume_reset_code(redis, email: str, code: str) -> str | None:
    """Exchange a code for the account it belongs to, or None.

    Guessing is bounded by a per-address counter: ten wrong codes and the
    address is out of the reset flow until the window expires. The code itself
    is single use.
    """
    email = normalise_email(email)
    attempt_key = RESET_ATTEMPT_KEY.format(email=email)

    attempts = await redis.incr(attempt_key)
    if attempts == 1:
        await redis.expire(attempt_key, RESET_TTL_SECONDS)
    if attempts > RESET_MAX_ATTEMPTS:
        return None

    key = RESET_KEY.format(digest=_reset_digest(email, code))
    if not await redis.exists(key):
        return None

    await redis.delete(key)
    await redis.srem(RESET_INDEX_KEY.format(email=email), _reset_digest(email, code))
    await redis.delete(attempt_key)

    user = await get_by_email(redis, email)
    return user["user_id"] if user is not None else None


async def resolve_access_token(redis, token: str) -> dict:
    """Turn a bearer token into the user it belongs to, or raise InvalidToken.

    Checks the signature, the expiry, the token type, that the account still
    exists, and that the token's version matches the account's current one. The
    last check is what makes revocation immediate rather than waiting for expiry.
    """
    claims = auth.decode_token(token, auth.ACCESS)
    user = await get_by_id(redis, claims.user_id)
    if user is None:
        raise auth.InvalidToken("unknown user")
    if int(user["token_version"]) != claims.version:
        raise auth.InvalidToken("token has been revoked")
    return user
