"""Operator accounts for the admin API.

Admins are a separate store from walkers, and their tokens are a separate type
(``auth.ADMIN``). That separation is the whole design: a walker's access token
is refused by every admin route because ``decode_token`` checks ``typ``, and an
admin token is refused by the walk socket for the same reason. There is no role
claim to forge and no session that can be replayed across the boundary.

Keys:

* ``dogwalk:admins:<admin_id>``       - hash: email, password_hash, created_at, token_version
* ``dogwalk:admins:email:<email>``    - email -> admin_id, the uniqueness index
* ``dogwalk:admin:loginfail:<email>`` - hash: client address -> failed-login
                                        count, so a lockout only locks the address
                                        that produced the failures
* ``dogwalk:admin:session:<jti>``     - a live admin token id, with a TTL, so logout
                                        can revoke the one token instead of only
                                        dropping the cookie
* ``dogwalk:admin:audit``             - capped list of JSON audit entries

Admins are created with ``scripts/create_admin.py`` rather than through an
endpoint. An endpoint that creates admins has to be protected by an admin,
which is a bootstrapping problem with no good answer at this size — and the
existing invite script already establishes "operator actions are scripts".
"""

import json
import logging
import time
import uuid

from app import account_requests, auth, email, users

logger = logging.getLogger(__name__)

ADMIN_KEY = "dogwalk:admins:{admin_id}"
ADMIN_EMAIL_KEY = "dogwalk:admins:email:{email}"
LOGIN_FAIL_KEY = "dogwalk:admin:loginfail:{email}"
SESSION_KEY = "dogwalk:admin:session:{jti}"
AUDIT_KEY = "dogwalk:admin:audit"

# Tighter than the walker lockout. There are a handful of admin accounts and
# each one can delete every account in the deployment, so five guesses is
# already generous.
MAX_LOGIN_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 900

# The audit list is capped rather than unbounded: it exists to answer "who did
# that" for recent actions, not to be a compliance archive.
AUDIT_MAX_ENTRIES = 1000

# Verified against when the address has no admin, so that "no such admin" and
# "wrong password" cost the same. Computed once at import.
_DUMMY_PASSWORD_HASH = auth.hash_password("mikywalk-dummy-admin-password-not-real")


class EmailTaken(Exception):
    """An admin with this email already exists."""


class InvalidCredentials(Exception):
    """Wrong email or password. Deliberately does not say which."""


class LockedOut(Exception):
    """Too many failed logins for this admin; try again later."""


class SamePassword(Exception):
    """The new password is the current one, so nothing would change."""


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def create_admin(redis, email: str, password: str) -> dict:
    email = normalise_email(email)
    if await redis.exists(ADMIN_EMAIL_KEY.format(email=email)):
        raise EmailTaken(email)

    admin_id = uuid.uuid4().hex
    record = {
        "email": email,
        "password_hash": await auth.hash_password_async(password),
        "created_at": int(time.time()),
        "token_version": 1,
    }
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(ADMIN_KEY.format(admin_id=admin_id), mapping=record)
        pipe.set(ADMIN_EMAIL_KEY.format(email=email), admin_id)
        await pipe.execute()

    return {"admin_id": admin_id, **record}


async def get_by_id(redis, admin_id: str) -> dict | None:
    record = await redis.hgetall(ADMIN_KEY.format(admin_id=admin_id))
    if not record:
        return None
    return {"admin_id": admin_id, **record}


async def get_by_email(redis, email: str) -> dict | None:
    admin_id = await redis.get(ADMIN_EMAIL_KEY.format(email=normalise_email(email)))
    if admin_id is None:
        return None
    return await get_by_id(redis, admin_id)


async def authenticate(redis, email: str, password: str, client_ip: str = "") -> dict:
    """Verify admin credentials, counting failures so an email cannot be guessed.

    The counter is a hash keyed by client address, same as the walker lockout:
    a lockout only locks the address that produced the failures.
    """
    email = normalise_email(email)
    fail_key = LOGIN_FAIL_KEY.format(email=email)
    field = client_ip or "unknown"

    failures = await redis.hget(fail_key, field)
    if failures is not None and int(failures) >= MAX_LOGIN_FAILURES:
        raise LockedOut(email)

    admin = await get_by_email(redis, email)

    # Always run the hash, even when the address is unknown, so the two failure
    # modes take the same time. Same reasoning as users.authenticate().
    stored = admin["password_hash"] if admin is not None else _DUMMY_PASSWORD_HASH
    password_ok = await auth.verify_password_async(password, stored)

    if admin is None or not password_ok:
        count = await redis.hincrby(fail_key, field, 1)
        if count == 1:
            await redis.expire(fail_key, LOGIN_FAILURE_WINDOW_SECONDS)
        raise InvalidCredentials(email)

    await redis.hdel(fail_key, field)
    return admin


async def change_password(
    redis, operator: dict, current_password: str, new_password: str, client_ip: str = ""
) -> int:
    """Replace an operator's password and sign out every other session.

    The current password is required even though the caller already holds a
    valid admin token. Without it a stolen token is enough to take the account
    over and lock the real operator out, which is the one thing a password
    change exists to prevent. It goes through ``authenticate`` so a wrong guess
    counts toward the same lockout as a wrong login.

    Returns the new ``token_version``. Every token minted before this call is
    dead, including the one that authorised it; the caller re-issues from the
    returned version, which is what keeps the session that made the change
    alive while every other one drops.
    """
    admin = await authenticate(redis, operator["email"], current_password, client_ip)

    if await auth.verify_password_async(new_password, admin["password_hash"]):
        raise SamePassword()

    key = ADMIN_KEY.format(admin_id=operator["admin_id"])
    new_hash = await auth.hash_password_async(new_password)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, "password_hash", new_hash)
        pipe.hincrby(key, "token_version", 1)
        results = await pipe.execute()

    await record(redis, operator, "change_password")
    return int(results[1])


async def register_session(redis, jti: str, ttl_seconds: int) -> None:
    """Remember a live admin token id, so logout can revoke it server-side."""
    await redis.set(SESSION_KEY.format(jti=jti), "1", ex=ttl_seconds)


async def revoke_session(redis, jti: str) -> None:
    """Drop one admin token id. Tokens issued before sessions existed have no
    ``jti`` and are refused outright; their holders simply sign in again."""
    await redis.delete(SESSION_KEY.format(jti=jti))


async def resolve_admin_token(redis, token: str) -> dict:
    """Turn a bearer token into the admin it belongs to, or raise InvalidToken.

    The type check is what keeps a walker token out: ``decode_token`` refuses
    anything whose ``typ`` is not ``admin``, so there is no path from a stolen
    user session to this API. The session check is what makes logout real:
    a token whose session key is gone is refused even before its expiry.
    """
    claims = auth.decode_token(token, auth.ADMIN)
    if claims.jti is None or not await redis.exists(SESSION_KEY.format(jti=claims.jti)):
        raise auth.InvalidToken("admin session has been revoked")
    admin = await get_by_id(redis, claims.user_id)
    if admin is None:
        raise auth.InvalidToken("unknown admin")
    if int(admin["token_version"]) != claims.version:
        raise auth.InvalidToken("admin token has been revoked")
    return admin


async def record(redis, admin: dict, action: str, target: str = "", detail: str = "") -> None:
    """Append one audit entry.

    Invite codes and passwords are never passed in here. An audit log that
    contains the secrets it is auditing is a second copy of them.
    """
    entry = {
        "ts": int(time.time()),
        "admin_id": admin["admin_id"],
        "admin_email": admin["email"],
        "action": action,
        "target": target,
        "detail": detail,
    }
    async with redis.pipeline(transaction=True) as pipe:
        pipe.lpush(AUDIT_KEY, json.dumps(entry))
        pipe.ltrim(AUDIT_KEY, 0, AUDIT_MAX_ENTRIES - 1)
        await pipe.execute()


async def recent(redis, limit: int = 50) -> list[dict]:
    """Newest first."""
    raw = await redis.lrange(AUDIT_KEY, 0, max(limit - 1, 0))
    entries = []
    for item in raw:
        if isinstance(item, bytes):
            item = item.decode()
        try:
            entries.append(json.loads(item))
        except ValueError:
            # A truncated or hand-edited entry should not take the page down.
            continue
    return entries


# The account mutations live here rather than in a route module because both the
# JSON API and the HTML pages perform them, and a second copy is how the two
# surfaces drift apart. The two that address an existing account return False
# when there is no such account.


async def create_account(redis, operator: dict, email: str) -> tuple[dict, str]:
    """Create a walker account with a generated password. Returns ``(account, password)``.

    No invite code is spent. The code exists to gate *self*-registration, where
    the caller is a stranger who has to prove they were invited; here the caller
    is an authenticated operator, and the audit entry is what records that the
    account was made. Requiring a code would only mean minting one to burn it
    immediately.

    The password is generated rather than chosen, and returned exactly once —
    only its hash goes into Redis. The account is flagged so the first sign-in
    can do nothing but replace it. That is the whole point: the operator knows
    this password, so a copy that stayed valid would not be a password, it would
    be a backdoor with a nicer name. The password never reaches the audit log.
    """
    password = auth.generate_password()
    account = await users.create_user(redis, email, password, must_change_password=True)
    await record(
        redis, operator, "create_user", target=account["user_id"], detail=account["email"]
    )
    return account, password


async def approve_request(
    redis, operator: dict, request_id: str, email_sender
) -> tuple[dict, bool] | None:
    """Turn a pending request into an account, and tell the requester.

    The account is created with a generated password that is thrown away. The
    operator never sees it, so it cannot become a shared secret, and the only
    way in is the reset flow — which is the account choosing its own password.
    That is also why the email carries no credential: a password in a mail body
    would outlive the fifteen minutes a reset code is good for.

    Returns ``(account, email_sent)``, or ``None`` when there is no such
    request. A failed send is not a failed approval: the account exists, and
    "Forgot password" works whether or not the notice arrived.
    """
    request_record = await account_requests.get(redis, request_id)
    if request_record is None:
        return None

    try:
        account, _password = await create_account(redis, operator, request_record["email"])
    except users.EmailTaken:
        # An invite code or another operator got there first. The request is
        # moot, so it goes rather than sitting in the queue forever.
        await account_requests.remove(redis, request_id)
        raise

    await account_requests.remove(redis, request_id)

    sent = True
    try:
        await email_sender.send(
            to=account["email"],
            subject="Your Mikywalk account has been approved",
            body=email.approval_email_body(),
        )
    except Exception:
        sent = False
        logger.exception("could not send the approval email for %s", account["user_id"])

    await record(
        redis, operator, "approve_request", target=account["user_id"], detail=account["email"]
    )
    return account, sent


async def reject_request(redis, operator: dict, request_id: str) -> bool:
    """Drop a request without creating anything. False when it is already gone."""
    request_record = await account_requests.remove(redis, request_id)
    if request_record is None:
        return False

    await record(redis, operator, "reject_request", detail=request_record["email"])
    return True


async def revoke_sessions(redis, hub, operator: dict, user_id: str) -> bool:
    """Sign an account out everywhere.

    Bumping ``token_version`` invalidates every token already issued, but a
    socket that is already open authenticated once and would keep running, so
    it is closed explicitly.
    """
    if await users.get_by_id(redis, user_id) is None:
        return False

    await users.bump_token_version(redis, user_id)
    if hub is not None:
        await hub.close(user_id, code=4401, reason="sessions revoked")

    await record(redis, operator, "revoke_sessions", target=user_id)
    return True


async def delete_account(redis, hub, operator: dict, user_id: str) -> bool:
    account = await users.get_by_id(redis, user_id)
    if account is None:
        return False

    if hub is not None:
        await hub.close(user_id, code=4401, reason="account deleted")

    await users.delete_user(redis, user_id)

    # The address is the only part of the record that is gone afterwards, so it
    # goes into the entry. The password hash never does.
    await record(redis, operator, "delete_user", target=user_id, detail=account["email"])
    return True
