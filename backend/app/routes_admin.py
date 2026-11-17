"""Admin API: accounts and invite codes.

Two rules shape this module.

**No positions.** The product's promise is that exact coordinates never leave
the server (see ``app/privacy.py``). Nothing here reads the geo index, and no
response carries a location, a distance or a bearing. An operator can see that
an account exists and can sign it out; they cannot watch it walk.

**Separate identity.** Admin tokens are ``auth.ADMIN``, a different ``typ`` from
a walker's access token, so neither can be presented where the other is
expected. There is no role claim on a user token to forge.

Auth is a dependency rather than a call at the top of each handler. That is not
only less repetition: FastAPI resolves dependencies before it validates the
request body, so an unauthenticated caller gets 401 rather than a 422 that
describes the shape of a payload they are not allowed to send.

Every mutating route writes an audit entry. The log is capped and readable by
every admin, so it records *that* codes were minted, never the codes.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app import (
    account_requests,
    admin,
    auth,
    config,
    email,
    invites,
    ratelimit,
    smtp_settings,
    users,
)

router = APIRouter(prefix="/admin", tags=["admin"])

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AdminCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)


class AdminTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    admin_id: str
    email: str


class ChangeAdminPasswordRequest(BaseModel):
    """The current password is required, not optional.

    An admin token alone must not be enough to take the account over: a stolen
    cookie would otherwise be a permanent takeover, and the real operator would
    be locked out of their own panel.
    """

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UserSummary(BaseModel):
    user_id: str
    email: str
    created_at: int
    sessions: int


class UserPage(BaseModel):
    total: int
    offset: int
    limit: int
    users: list[UserSummary]


class UserDetail(BaseModel):
    user_id: str
    email: str
    created_at: int
    token_version: int
    sessions: int


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)


class CreatedUser(BaseModel):
    """The new account, plus the one-time password that goes with it.

    ``temporary_password`` is the only time this value exists outside the
    operator's hands: only its hash is stored. The account cannot use it for
    anything except setting a real password, so the response is a handover
    note, not a credential.
    """

    user_id: str
    email: str
    created_at: int
    temporary_password: str


class InviteSummary(BaseModel):
    unused: int
    used: int
    codes: list[str]


class AccountRequestSummary(BaseModel):
    request_id: str
    email: str
    created_at: int


class AccountRequestPage(BaseModel):
    total: int
    requests: list[AccountRequestSummary]


class ApprovedRequest(BaseModel):
    """The account an approval created.

    No password: it was generated and discarded, so the requester sets their own
    through the reset flow. ``email_sent`` is false when the provider refused —
    the approval still stands, and "Forgot password" works regardless.
    """

    user_id: str
    email: str
    created_at: int
    email_sent: bool


class MintInvitesRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=50)


class MintedInvites(BaseModel):
    codes: list[str]
    unused: int


class Stats(BaseModel):
    accounts: int
    invites_unused: int
    invites_used: int


class AuditEntry(BaseModel):
    ts: int
    admin_id: str
    admin_email: str
    action: str
    target: str = ""
    detail: str = ""


class AuditPage(BaseModel):
    entries: list[AuditEntry]


class SmtpSettings(BaseModel):
    """The SMTP configuration as the panel edits it.

    ``password`` is write-only. An empty value on save means "keep the stored
    one", which is what lets the form redraw without ever carrying the secret
    back to the browser.
    """

    host: str = Field(default="", max_length=253)
    port: int = Field(default=587, ge=1, le=65535)
    username: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=512)
    sender: str = Field(default="", max_length=254)
    security: str = Field(default="starttls")


class SmtpSettingsView(BaseModel):
    host: str
    port: int
    username: str
    sender: str
    security: str
    has_password: bool
    # "panel" when an operator saved a configuration, "environment" when it comes
    # from SMTP_* variables, "none" when neither is set up.
    source: str
    # Whether a real sender is in use right now, as opposed to the log.
    active: bool


class SmtpTestRequest(SmtpSettings):
    to: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)


class SmtpTestResult(BaseModel):
    ok: bool
    detail: str


def _redis(request: Request):
    return request.app.state.redis


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token


async def require_admin(request: Request) -> dict:
    """The admin behind the bearer token, or 401.

    A walker's access token fails here on the type check alone, before any
    lookup, which is the property that keeps the two APIs apart.
    """
    try:
        return await admin.resolve_admin_token(_redis(request), _bearer(request))
    except auth.InvalidToken:
        raise HTTPException(status_code=401, detail="invalid admin token")


@router.post("/auth/login", response_model=AdminTokenResponse)
async def login(credentials: AdminCredentials, request: Request) -> AdminTokenResponse:
    redis = _redis(request)
    try:
        operator = await admin.authenticate(
            redis,
            credentials.email,
            credentials.password,
            client_ip=ratelimit.client_identity(request),
        )
    except admin.LockedOut:
        raise HTTPException(status_code=429, detail="too many failed attempts")
    except admin.InvalidCredentials:
        raise HTTPException(status_code=401, detail="invalid email or password")

    token, ttl = auth.issue_admin_token(operator["admin_id"], int(operator["token_version"]))
    await admin.record(redis, operator, "login")
    return AdminTokenResponse(
        access_token=token,
        expires_in=ttl,
        admin_id=operator["admin_id"],
        email=operator["email"],
    )


@router.post("/auth/password", response_model=AdminTokenResponse)
async def change_password(
    body: ChangeAdminPasswordRequest,
    request: Request,
    operator: dict = Depends(require_admin),
) -> AdminTokenResponse:
    """Replace the operator's password and sign out every other session.

    Returns a fresh token rather than 204. The change bumps ``token_version``,
    which kills the token that authorised it, so handing back a replacement is
    what keeps the caller signed in while every other device drops. That is the
    same shape as the walker's ``/auth/password/change``.
    """
    redis = _redis(request)
    try:
        version = await admin.change_password(
            redis,
            operator,
            body.current_password,
            body.new_password,
            client_ip=ratelimit.client_identity(request),
        )
    except admin.LockedOut:
        raise HTTPException(status_code=429, detail="too many failed attempts")
    except admin.InvalidCredentials:
        # 400 rather than 401: the caller is authenticated, it is the
        # confirmation that failed. A 401 would tell a browser to sign out.
        raise HTTPException(status_code=400, detail="current password is wrong")
    except admin.SamePassword:
        raise HTTPException(
            status_code=400, detail="choose a password different from the current one"
        )

    token, ttl = auth.issue_admin_token(operator["admin_id"], version)
    return AdminTokenResponse(
        access_token=token,
        expires_in=ttl,
        admin_id=operator["admin_id"],
        email=operator["email"],
    )


@router.get("/users", response_model=UserPage)
async def list_users(
    request: Request,
    _operator: dict = Depends(require_admin),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> UserPage:
    redis = _redis(request)

    records = await users.list_users(redis, offset, limit)
    counts = await users.session_counts(redis, [record["user_id"] for record in records])

    return UserPage(
        total=await users.count_users(redis),
        offset=offset,
        limit=limit,
        users=[
            UserSummary(
                user_id=record["user_id"],
                email=record["email"],
                created_at=int(record["created_at"]),
                sessions=counts.get(record["user_id"], 0),
            )
            for record in records
        ],
    )


@router.post("/users", response_model=CreatedUser, status_code=201)
async def create_user(
    body: CreateUserRequest,
    request: Request,
    operator: dict = Depends(require_admin),
) -> CreatedUser:
    """Create an account directly, without spending an invite code.

    The invite gate protects registration from strangers. An operator is not a
    stranger, so the gate does not apply — see ``admin.create_account``. The
    password is generated and returned once; the account must replace it at
    first sign-in.
    """
    try:
        account, password = await admin.create_account(_redis(request), operator, body.email)
    except users.EmailTaken:
        raise HTTPException(status_code=409, detail="email already registered")

    return CreatedUser(
        user_id=account["user_id"],
        email=account["email"],
        created_at=int(account["created_at"]),
        temporary_password=password,
    )


@router.get("/users/{user_id}", response_model=UserDetail)
async def get_user(
    user_id: str,
    request: Request,
    _operator: dict = Depends(require_admin),
) -> UserDetail:
    redis = _redis(request)

    record = await users.get_by_id(redis, user_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such account")

    counts = await users.session_counts(redis, [user_id])
    return UserDetail(
        user_id=record["user_id"],
        email=record["email"],
        created_at=int(record["created_at"]),
        token_version=int(record["token_version"]),
        sessions=counts.get(user_id, 0),
    )


@router.post("/users/{user_id}/revoke-sessions", status_code=204)
async def revoke_sessions(
    user_id: str,
    request: Request,
    operator: dict = Depends(require_admin),
) -> Response:
    """Sign an account out everywhere."""
    if not await admin.revoke_sessions(
        _redis(request), getattr(request.app.state, "hub", None), operator, user_id
    ):
        raise HTTPException(status_code=404, detail="no such account")
    return Response(status_code=204)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    request: Request,
    operator: dict = Depends(require_admin),
) -> Response:
    if not await admin.delete_account(
        _redis(request), getattr(request.app.state, "hub", None), operator, user_id
    ):
        raise HTTPException(status_code=404, detail="no such account")
    return Response(status_code=204)


@router.get("/invites", response_model=InviteSummary)
async def list_invites(
    request: Request,
    _operator: dict = Depends(require_admin),
) -> InviteSummary:
    redis = _redis(request)

    unused = await redis.smembers(invites.UNUSED_KEY)
    return InviteSummary(
        unused=len(unused),
        used=int(await redis.scard(invites.USED_KEY)),
        codes=sorted(invites.format_code(code) for code in unused),
    )


@router.post("/invites", response_model=MintedInvites, status_code=201)
async def mint_invites(
    body: MintInvitesRequest,
    request: Request,
    operator: dict = Depends(require_admin),
) -> MintedInvites:
    redis = _redis(request)

    codes = await invites.mint(redis, body.count)
    await admin.record(redis, operator, "mint_invites", detail=f"{len(codes)} code(s)")
    return MintedInvites(codes=codes, unused=await invites.available(redis))


@router.get("/requests", response_model=AccountRequestPage)
async def list_requests(
    request: Request,
    _operator: dict = Depends(require_admin),
) -> AccountRequestPage:
    """Accounts strangers asked for, newest first. Nothing here is an account yet."""
    redis = _redis(request)

    records = await account_requests.pending(redis)
    return AccountRequestPage(
        total=await account_requests.count(redis),
        requests=[
            AccountRequestSummary(
                request_id=record["request_id"],
                email=record["email"],
                created_at=int(record["created_at"]),
            )
            for record in records
        ],
    )


@router.post("/requests/{request_id}/approve", response_model=ApprovedRequest, status_code=201)
async def approve_request(
    request_id: str,
    request: Request,
    operator: dict = Depends(require_admin),
) -> ApprovedRequest:
    """Create the account and email the requester.

    The account gets a generated password that is discarded, so the only way in
    is the reset flow. See ``admin.approve_request``.
    """
    try:
        outcome = await admin.approve_request(
            _redis(request), operator, request_id, request.app.state.email_sender
        )
    except users.EmailTaken:
        raise HTTPException(status_code=409, detail="email already registered")

    if outcome is None:
        raise HTTPException(status_code=404, detail="no such request")

    account, sent = outcome
    return ApprovedRequest(
        user_id=account["user_id"],
        email=account["email"],
        created_at=int(account["created_at"]),
        email_sent=sent,
    )


@router.delete("/requests/{request_id}", status_code=204)
async def reject_request(
    request_id: str,
    request: Request,
    operator: dict = Depends(require_admin),
) -> Response:
    if not await admin.reject_request(_redis(request), operator, request_id):
        raise HTTPException(status_code=404, detail="no such request")
    return Response(status_code=204)


@router.get("/stats", response_model=Stats)
async def stats(
    request: Request,
    _operator: dict = Depends(require_admin),
) -> Stats:
    redis = _redis(request)

    return Stats(
        accounts=await users.count_users(redis),
        invites_unused=await invites.available(redis),
        invites_used=int(await redis.scard(invites.USED_KEY)),
    )


@router.get("/audit", response_model=AuditPage)
async def audit_log(
    request: Request,
    _operator: dict = Depends(require_admin),
    limit: int = Query(50, ge=1, le=200),
) -> AuditPage:
    entries = await admin.recent(_redis(request), limit)
    return AuditPage(entries=[AuditEntry(**entry) for entry in entries])


# --- outbound email -------------------------------------------------------


def _smtp_config(
    body: SmtpSettings, stored: smtp_settings.SmtpConfig | None
) -> smtp_settings.SmtpConfig:
    """The submitted values, with the stored password when the field was blank."""
    return smtp_settings.SmtpConfig(
        host=body.host.strip(),
        port=body.port,
        sender=body.sender.strip(),
        username=body.username.strip(),
        password=body.password or (stored.password if stored is not None else ""),
        security=body.security,
    )


@router.get("/smtp", response_model=SmtpSettingsView)
async def read_smtp(
    request: Request,
    _operator: dict = Depends(require_admin),
) -> SmtpSettingsView:
    """What the panel should show. Never the password, only whether one is set."""
    redis = _redis(request)
    stored = await smtp_settings.load(redis)
    environment = smtp_settings.from_settings(config.settings)

    if stored is not None:
        source, shown = "panel", stored
    elif config.settings.email_sender == "smtp":
        source, shown = "environment", environment
    else:
        source, shown = "none", environment

    return SmtpSettingsView(
        host=shown.host,
        port=shown.port,
        username=shown.username,
        sender=shown.sender,
        security=shown.security,
        has_password=bool(shown.password),
        source=source,
        active=await smtp_settings.resolve(redis, config.settings) is not None,
    )


@router.put("/smtp", response_model=SmtpSettingsView)
async def save_smtp(
    body: SmtpSettings,
    request: Request,
    operator: dict = Depends(require_admin),
) -> SmtpSettingsView:
    """Store a configuration and switch the running sender to it.

    The rebuild is what makes this useful: the next password-reset email goes
    through the new server without a restart.
    """
    redis = _redis(request)
    candidate = _smtp_config(body, await smtp_settings.load(redis))

    problem = candidate.problem()
    if problem is not None:
        raise HTTPException(status_code=400, detail=problem)

    await smtp_settings.save(redis, candidate)
    await email.refresh(request.app)
    await admin.record(
        redis,
        operator,
        "smtp_saved",
        target=candidate.host,
        detail=f"{candidate.sender} via {candidate.security}",
    )
    return await read_smtp(request, operator)


@router.delete("/smtp", status_code=204)
async def clear_smtp(
    request: Request,
    operator: dict = Depends(require_admin),
) -> Response:
    """Forget the stored configuration and fall back to EMAIL_SENDER."""
    redis = _redis(request)
    await smtp_settings.clear(redis)
    await email.refresh(request.app)
    await admin.record(redis, operator, "smtp_cleared")
    return Response(status_code=204)


@router.post("/smtp/test", response_model=SmtpTestResult)
async def test_smtp(
    body: SmtpTestRequest,
    request: Request,
    operator: dict = Depends(require_admin),
) -> SmtpTestResult:
    """Send one message through the submitted configuration, without saving it.

    Testing before saving is the point: a wrong port or a rejected sender is
    something to find out here, not by breaking password reset for everyone.
    """
    redis = _redis(request)
    candidate = _smtp_config(body, await smtp_settings.load(redis))

    problem = candidate.problem()
    if problem is not None:
        return SmtpTestResult(ok=False, detail=problem)

    sender = email.SmtpEmailSender(
        host=candidate.host,
        port=candidate.port,
        sender=candidate.sender,
        username=candidate.username,
        password=candidate.password,
        security=candidate.security,
        timeout=candidate.timeout,
    )
    try:
        await sender.send(
            to=body.to,
            subject="DogWalk SMTP test",
            body=email.test_email_body(candidate.host),
        )
    except Exception as error:
        # The message is the useful part: "connection refused" and "authentication
        # failed" send an operator to different places.
        await admin.record(
            redis, operator, "smtp_test", target=body.to, detail=f"failed: {type(error).__name__}"
        )
        return SmtpTestResult(ok=False, detail=f"{type(error).__name__}: {error}")

    await admin.record(redis, operator, "smtp_test", target=body.to, detail="delivered")
    return SmtpTestResult(ok=True, detail=f"Sent to {body.to} through {candidate.host}.")
