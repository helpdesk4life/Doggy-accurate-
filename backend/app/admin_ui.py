"""Server-rendered admin pages.

The JSON API in ``routes_admin.py`` is the contract; this is a thin HTML skin
over the same store functions, so the two cannot drift. There is no build step
and no JavaScript: every action is a form post followed by a redirect, which is
the smallest thing that works in a phone browser and needs no toolchain this
repository does not already have.

Auth is a cookie rather than the ``Authorization`` header, because a browser
form cannot set a header. The cookie is HttpOnly, SameSite=Strict and scoped to
``/admin``. SameSite=Strict is what stands in for a CSRF token: a form on
another origin does not get the cookie attached, so it cannot drive these
routes. If this ever has to work across sites, that is the piece to revisit.

Nothing here reads the geo index. The pages show accounts and invite codes, and
that is the whole surface — see the note at the top of ``routes_admin.py``.
"""

import datetime
import json
import pathlib
import re
import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import admin, auth, invites, users
from app.routes_admin import EMAIL_PATTERN

router = APIRouter(prefix="/admin/ui", tags=["admin-ui"], include_in_schema=False)

COOKIE = "dogwalk_admin"
FLASH_KEY = "dogwalk:admin:flash:{flash_id}"
FLASH_TTL_SECONDS = 120
PAGE_SIZE = 25

# The same rule the JSON API enforces through pydantic. Imported rather than
# copied: a third copy of this regex is how the two surfaces start disagreeing
# about what an address is.
_EMAIL_RE = re.compile(EMAIL_PATTERN)

templates = Jinja2Templates(directory=str(pathlib.Path(__file__).resolve().parent / "templates"))


def _when(ts) -> str:
    """Unix seconds as a UTC timestamp. The server has no idea of your timezone."""
    return datetime.datetime.fromtimestamp(int(ts), datetime.UTC).strftime("%Y-%m-%d %H:%M")


templates.env.filters["when"] = _when


def _secure(request: Request) -> bool:
    """Mark the cookie Secure whenever the browser reached us over TLS."""
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


async def _current(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        return await admin.resolve_admin_token(request.app.state.redis, token)
    except auth.InvalidToken:
        return None


def _to_login() -> RedirectResponse:
    return RedirectResponse("/admin/ui/login", status_code=303)


def _page(request: Request, name: str, context: dict, status_code: int = 200):
    return templates.TemplateResponse(request, f"admin/{name}.html", context, status_code=status_code)


async def _flash_put(redis, payload: dict) -> str:
    """Stash a one-shot payload and return the id that fetches it.

    Invite codes and handed-over passwords come back through this rather than a
    query string: a query string lands in the proxy access log, and these are
    secrets. The key is deleted on read, so a refresh does not show them twice.
    """
    flash_id = uuid.uuid4().hex
    await redis.set(
        FLASH_KEY.format(flash_id=flash_id), json.dumps(payload), ex=FLASH_TTL_SECONDS
    )
    return flash_id


async def _flash_take(redis, flash_id: str | None) -> dict:
    """Read and burn. Empty when the id is unknown, expired or already spent."""
    if not flash_id:
        return {}
    key = FLASH_KEY.format(flash_id=flash_id)
    raw = await redis.get(key)
    if not raw:
        return {}
    await redis.delete(key)
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await _current(request) is not None:
        return RedirectResponse("/admin/ui", status_code=303)
    return _page(request, "login", {"admin": None, "error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    redis = request.app.state.redis
    try:
        operator = await admin.authenticate(redis, email, password)
    except (admin.InvalidCredentials, admin.LockedOut):
        # One message for both, and no hint about which half was wrong.
        return _page(
            request,
            "login",
            {"admin": None, "error": "That email and password do not match an operator account."},
            status_code=401,
        )

    token, ttl = auth.issue_admin_token(operator["admin_id"], int(operator["token_version"]))
    await admin.record(redis, operator, "login")

    response = RedirectResponse("/admin/ui", status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        max_age=ttl,
        httponly=True,
        samesite="strict",
        secure=_secure(request),
        path="/admin",
    )
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/admin/ui/login", status_code=303)
    response.delete_cookie(COOKIE, path="/admin")
    return response


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    return _page(
        request,
        "dashboard",
        {
            "admin": operator,
            "page": "dashboard",
            "accounts": await users.count_users(redis),
            "invites_unused": await invites.available(redis),
            "invites_used": int(await redis.scard(invites.USED_KEY)),
            "entries": await admin.recent(redis, 10),
        },
    )


async def _users_context(redis, operator: dict, offset: int) -> dict:
    """Everything the accounts page needs, so a failed form post can redraw it."""
    records = await users.list_users(redis, offset, PAGE_SIZE)
    counts = await users.session_counts(redis, [record["user_id"] for record in records])
    total = await users.count_users(redis)

    return {
        "admin": operator,
        "page": "users",
        "users": [
            {
                "user_id": record["user_id"],
                "email": record["email"],
                "created_at": int(record["created_at"]),
                "sessions": counts.get(record["user_id"], 0),
            }
            for record in records
        ],
        "total": total,
        "offset": offset,
        "limit": PAGE_SIZE,
        "prev_offset": max(offset - PAGE_SIZE, 0),
        "next_offset": offset + PAGE_SIZE,
        "has_next": offset + PAGE_SIZE < total,
    }


def _new_account_problem(email: str) -> str | None:
    """The API's rule, as a sentence a person can act on.

    The API answers a bad payload with a 422 and a schema. A form has to say
    what is wrong in words, so the check is repeated here rather than delegated
    to pydantic — but the rule itself comes from one place. There is nothing to
    check about the password: the server generates it.
    """
    if not _EMAIL_RE.match(email.strip()):
        return "That does not look like an email address."
    return None


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, offset: int = 0, flash: str | None = None):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    context = await _users_context(redis, operator, max(offset, 0))
    context["created"] = await _flash_take(redis, flash)
    context["error"] = None
    context["form_email"] = ""
    return _page(request, "users", context)


@router.post("/users")
async def create_user(request: Request, email: str = Form("")):
    """Create an account from the panel.

    The password is generated server-side and shown once, on the redirect that
    follows. A failure redraws the page with the message rather than
    redirecting, so the operator does not lose what they typed.
    """
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    problem = _new_account_problem(email)

    if problem is None:
        try:
            account, password = await admin.create_account(redis, operator, email)
        except users.EmailTaken:
            problem = "That address already has an account."
        else:
            flash_id = await _flash_put(
                redis, {"email": account["email"], "password": password}
            )
            return RedirectResponse(f"/admin/ui/users?flash={flash_id}", status_code=303)

    context = await _users_context(redis, operator, 0)
    context["created"] = {}
    context["error"] = problem
    context["form_email"] = email.strip()
    return _page(request, "users", context, status_code=400)


@router.post("/users/{user_id}/revoke")
async def revoke(request: Request, user_id: str, offset: int = Form(0)):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    await admin.revoke_sessions(
        request.app.state.redis,
        getattr(request.app.state, "hub", None),
        operator,
        user_id,
    )
    return RedirectResponse(f"/admin/ui/users?offset={max(offset, 0)}", status_code=303)


@router.get("/users/{user_id}/delete", response_class=HTMLResponse)
async def confirm_delete(request: Request, user_id: str, offset: int = 0):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    account = await users.get_by_id(request.app.state.redis, user_id)
    if account is None:
        return RedirectResponse("/admin/ui/users", status_code=303)

    return _page(
        request,
        "confirm_delete",
        {
            "admin": operator,
            "page": "users",
            "account": {"user_id": account["user_id"], "email": account["email"]},
            "offset": max(offset, 0),
        },
    )


@router.post("/users/{user_id}/delete")
async def delete(request: Request, user_id: str, offset: int = Form(0)):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    await admin.delete_account(
        request.app.state.redis,
        getattr(request.app.state, "hub", None),
        operator,
        user_id,
    )
    return RedirectResponse(f"/admin/ui/users?offset={max(offset, 0)}", status_code=303)


@router.get("/invites", response_class=HTMLResponse)
async def invites_page(request: Request, flash: str | None = None):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis

    # The codes are read back from a short-lived key rather than passed in the
    # URL: a query string lands in the proxy access log, and these are secrets.
    minted: list[str] = (await _flash_take(redis, flash)).get("codes", [])

    unused = await redis.smembers(invites.UNUSED_KEY)
    return _page(
        request,
        "invites",
        {
            "admin": operator,
            "page": "invites",
            "codes": sorted(invites.format_code(code) for code in unused),
            "used": int(await redis.scard(invites.USED_KEY)),
            "minted": minted,
        },
    )


@router.post("/invites")
async def mint(request: Request, count: int = Form(1)):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    codes = await invites.mint(redis, min(max(count, 1), 50))
    await admin.record(redis, operator, "mint_invites", detail=f"{len(codes)} code(s)")

    flash_id = await _flash_put(redis, {"codes": codes})
    return RedirectResponse(f"/admin/ui/invites?flash={flash_id}", status_code=303)


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    return _page(
        request,
        "audit",
        {
            "admin": operator,
            "page": "audit",
            "entries": await admin.recent(request.app.state.redis, 100),
        },
    )
