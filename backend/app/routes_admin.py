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

from app import admin, auth, invites, users

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
        operator = await admin.authenticate(redis, credentials.email, credentials.password)
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
