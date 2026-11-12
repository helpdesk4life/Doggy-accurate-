"""Registration, login, refresh, logout, password reset and account deletion.

Refresh tokens are single use: presenting one consumes it and returns a fresh
pair. That way a stolen refresh token is good for at most one exchange, and the
legitimate client's next refresh fails loudly instead of silently sharing a
session with an attacker. A replay outside a short grace window is treated as a
leak and revokes every session on the account.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import auth, config, email, invites, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    # Only read by /auth/register. Login ignores it, so the two endpoints can
    # share a model without the client having to send a code to sign in.
    invite_code: str | None = Field(default=None, max_length=64)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)


class PasswordResetConfirm(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    password: str = Field(min_length=8, max_length=128)


class DeleteAccountRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str


def _redis(request: Request):
    return request.app.state.redis


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token


async def _require_user(request: Request) -> dict:
    """The account behind the bearer token, or 401."""
    try:
        return await users.resolve_access_token(_redis(request), _bearer(request))
    except auth.InvalidToken:
        raise HTTPException(status_code=401, detail="invalid access token")


async def _issue(redis, user: dict) -> TokenResponse:
    pair = auth.issue_tokens(user["user_id"], int(user["token_version"]))
    await users.store_refresh(
        redis,
        pair.refresh_jti,
        user["user_id"],
        config.settings.refresh_token_ttl_seconds,
    )
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        user_id=user["user_id"],
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(credentials: Credentials, request: Request) -> TokenResponse:
    redis = _redis(request)

    # The invite check runs before the email lookup, and that order is the whole
    # point: an uninvited caller gets the same 403 whether or not the address
    # has an account, so this endpoint can no longer be used to find out who is
    # registered. See app/invites.py for why the status code alone cannot fix it.
    code = ""
    if request.app.state.invite_required:
        code = invites.normalise(credentials.invite_code or "")
        if not code:
            raise HTTPException(status_code=403, detail="an invite code is required to register")
        if not await invites.consume(redis, code):
            raise HTTPException(status_code=403, detail="that invite code is not valid")

    try:
        user = await users.create_user(redis, credentials.email, credentials.password)
    except users.EmailTaken:
        # The registration did not happen, so the code goes back rather than
        # being burnt on a mistyped address.
        if code:
            await invites.refund(redis, code)
        raise HTTPException(status_code=409, detail="email already registered")
    return await _issue(redis, user)


@router.post("/login", response_model=TokenResponse)
async def login(credentials: Credentials, request: Request) -> TokenResponse:
    redis = _redis(request)
    try:
        user = await users.authenticate(redis, credentials.email, credentials.password)
    except users.LockedOut:
        raise HTTPException(status_code=429, detail="too many failed attempts")
    except users.InvalidCredentials:
        raise HTTPException(status_code=401, detail="invalid email or password")
    return await _issue(redis, user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, request: Request) -> TokenResponse:
    redis = _redis(request)
    try:
        claims = auth.decode_token(body.refresh_token, auth.REFRESH)
    except auth.InvalidToken:
        raise HTTPException(status_code=401, detail="invalid refresh token")

    if claims.jti is None:
        raise HTTPException(status_code=401, detail="invalid refresh token")

    # Single use: consume before issuing, so a replay of this token fails.
    user_id = await users.consume_refresh(redis, claims.jti)
    if user_id is None:
        await _handle_refresh_replay(redis, claims.jti)
        raise HTTPException(status_code=401, detail="refresh token already used or revoked")

    if user_id != claims.user_id:
        raise HTTPException(status_code=401, detail="refresh token already used or revoked")

    user = await users.get_by_id(redis, user_id)
    if user is None or int(user["token_version"]) != claims.version:
        raise HTTPException(status_code=401, detail="refresh token has been revoked")

    return await _issue(redis, user)


async def _handle_refresh_replay(redis, jti: str) -> None:
    """Decide whether a rejected refresh id was a retry or a leak.

    A client that loses the response to a successful refresh retries with the
    same token. Signing that user out of everything would be a bad trade on a
    flaky connection, so a replay inside the grace window is just a 401. A later
    replay means two parties hold the same token, which is the case the rotation
    exists to catch: drop every session on the account.
    """
    replay = await users.replay_of_consumed_refresh(redis, jti)
    if replay is None:
        return

    user_id, age = replay
    if age <= users.REPLAY_GRACE_SECONDS:
        return

    await users.bump_token_version(redis, user_id)
    logger.warning(
        "refresh token replayed %ds after use for user %s; every session revoked",
        age,
        user_id,
    )


@router.post("/logout", status_code=204)
async def logout(body: RefreshRequest, request: Request) -> Response:
    redis = _redis(request)
    try:
        claims = auth.decode_token(body.refresh_token, auth.REFRESH)
    except auth.InvalidToken:
        # Logging out with a dead token is not an error worth reporting.
        return Response(status_code=204)
    if claims.jti is not None:
        await users.revoke_refresh(redis, claims.jti)
    return Response(status_code=204)


@router.post("/logout-all", status_code=204)
async def logout_all(request: Request) -> Response:
    """Bump the token version, invalidating every token on every device."""
    redis = _redis(request)
    user = await _require_user(request)
    await users.bump_token_version(redis, user["user_id"])
    return Response(status_code=204)


@router.post("/password-reset/request", status_code=202)
async def request_password_reset(body: PasswordResetRequest, request: Request) -> Response:
    """Send a reset code, if the address has an account.

    Always 202. A different status for a known address would turn this endpoint
    into an account enumeration oracle, which is the same reason login does not
    say which half of the credentials was wrong.
    """
    redis = _redis(request)
    user = await users.get_by_email(redis, body.email)

    if user is not None:
        code = await users.create_reset_code(redis, user["email"])
        await request.app.state.email_sender.send(
            to=user["email"],
            subject="Your DogWalk password reset code",
            body=email.reset_email_body(code, users.RESET_TTL_SECONDS // 60),
        )

    return Response(status_code=202)


@router.post("/password-reset/confirm", status_code=204)
async def confirm_password_reset(body: PasswordResetConfirm, request: Request) -> Response:
    redis = _redis(request)
    user_id = await users.consume_reset_code(redis, body.email, body.code)
    if user_id is None:
        raise HTTPException(status_code=400, detail="invalid or expired reset code")

    await users.set_password(redis, user_id, body.password)

    # A reset usually means the old password is compromised, so every existing
    # session goes with it. The user signs in again with the new password.
    await users.bump_token_version(redis, user_id)
    logger.info("password reset completed for user %s", user_id)
    return Response(status_code=204)


@router.delete("/account", status_code=204)
async def delete_account(body: DeleteAccountRequest, request: Request) -> Response:
    """Delete the account and everything attached to it.

    Requires the password as well as the token: a stolen phone with an unlocked
    session should not be enough to destroy someone's account.
    """
    redis = _redis(request)
    user = await _require_user(request)

    if not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid password")

    user_id = user["user_id"]

    # Drop the live socket first, so the walker stops being broadcast before the
    # account disappears underneath it.
    hub = getattr(request.app.state, "hub", None)
    if hub is not None:
        await hub.close(user_id, code=4401, reason="account deleted")

    await users.delete_user(redis, user_id)
    logger.info("account %s deleted", user_id)
    return Response(status_code=204)
