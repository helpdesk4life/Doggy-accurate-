"""Password hashing and JWT issuing/verification.

Passwords use stdlib ``scrypt`` with a per-user salt, so there is no native
dependency to build. Tokens are HS256 JWTs signed with ``JWT_SECRET``.

Two token types are issued:

* ``access``  - short lived, presented on the WebSocket handshake.
* ``refresh`` - long lived, exchanged for a new access token. Each carries a
  ``jti`` that is tracked in Redis, so one session can be revoked on its own.

Every token also carries the user's ``ver`` (token version). Bumping the stored
version invalidates every token that user has ever been issued, which is what
"log out everywhere" needs.
"""

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt

from app import config

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

ACCESS = "access"
REFRESH = "refresh"


class InvalidToken(Exception):
    """Raised when a token is malformed, expired, of the wrong type, or revoked."""


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_jti: str


@dataclass(frozen=True)
class Claims:
    user_id: str
    token_type: str
    version: int
    jti: str | None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )
    return "$".join(
        [
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(key).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _encode(user_id: str, token_type: str, version: int, ttl: int, jti: str | None) -> str:
    now = int(time.time())
    payload: dict = {
        "sub": user_id,
        "typ": token_type,
        "ver": version,
        "iat": now,
        "exp": now + ttl,
    }
    if jti is not None:
        payload["jti"] = jti
    return jwt.encode(payload, config.settings.jwt_secret, algorithm="HS256")


def issue_tokens(user_id: str, version: int) -> TokenPair:
    access_ttl = config.settings.access_token_ttl_seconds
    refresh_ttl = config.settings.refresh_token_ttl_seconds
    jti = uuid.uuid4().hex
    return TokenPair(
        access_token=_encode(user_id, ACCESS, version, access_ttl, None),
        refresh_token=_encode(user_id, REFRESH, version, refresh_ttl, jti),
        expires_in=access_ttl,
        refresh_jti=jti,
    )


def decode_token(token: str, expected_type: str) -> Claims:
    """Verify signature and expiry, then check the token is the type we expect."""
    try:
        payload = jwt.decode(token, config.settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc

    token_type = payload.get("typ")
    if token_type != expected_type:
        raise InvalidToken(f"expected a {expected_type} token, got {token_type!r}")

    user_id = payload.get("sub")
    version = payload.get("ver")
    if not isinstance(user_id, str) or not isinstance(version, int):
        raise InvalidToken("token is missing sub or ver")

    return Claims(
        user_id=user_id,
        token_type=token_type,
        version=version,
        jti=payload.get("jti"),
    )
