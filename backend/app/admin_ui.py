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
        operator = await admin.authenticate(
            redis, email, password, client_ip=ratelimit.client_identity(request)
        )
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


# --- the operator's own account -------------------------------------------


@router.get("/password", response_class=HTMLResponse)
async def password_page(request: Request, saved: int = 0):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    return _page(
        request,
        "password",
        {"admin": operator, "page": "password", "error": None, "saved": bool(saved)},
    )


@router.post("/password")
async def password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    """Change the operator's password, then keep this browser signed in.

    The confirmation field is checked here rather than in the store function
    because it is a typing mistake, not a rule about passwords: the JSON API
    has no second field and should not grow one.

    The cookie is reissued from the new ``token_version``. Without that, the
    change would sign out the very browser that made it, and the operator would
    land on the login page wondering whether it worked.
    """
    redis = request.app.state.redis
    operator = await _current(request)
    if operator is None:
        return _to_login()

    def rerender(error: str):
        return _page(
            request,
            "password",
            {"admin": operator, "page": "password", "error": error, "saved": False},
            status_code=400,
        )

    if new_password != confirm_password:
        return rerender("The two new passwords do not match.")
    if len(new_password) < 8:
        return rerender("Use at least 8 characters.")
    if len(new_password) > 128:
        return rerender("Use at most 128 characters.")

    try:
        version = await admin.change_password(
            redis,
            operator,
            current_password,
            new_password,
            client_ip=ratelimit.client_identity(request),
        )
    except admin.LockedOut:
        return rerender("Too many failed attempts. Try again in a few minutes.")
    except admin.InvalidCredentials:
        return rerender("That is not your current password.")
    except admin.SamePassword:
        return rerender("Choose a password different from the current one.")

    token, ttl = auth.issue_admin_token(operator["admin_id"], version)
    response = RedirectResponse("/admin/ui/password?saved=1", status_code=303)
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
            "invites_used": await invites.used_count(redis),
            "requests_pending": await account_requests.count(redis),
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
            "used": await invites.used_count(redis),
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


# --- account requests -----------------------------------------------------


async def _requests_context(redis, operator: dict) -> dict:
    """Everything the requests page needs, so a failed approval can redraw it."""
    records = await account_requests.pending(redis)
    return {
        "admin": operator,
        "page": "requests",
        "requests": [
            {
                "request_id": record["request_id"],
                "email": record["email"],
                "created_at": int(record["created_at"]),
            }
            for record in records
        ],
        "total": await account_requests.count(redis),
        "approved": {},
        "error": None,
    }


@router.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request, flash: str | None = None):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    context = await _requests_context(redis, operator)
    context["approved"] = await _flash_take(redis, flash)
    return _page(request, "requests", context)


@router.post("/requests/{request_id}/approve")
async def approve_request(request: Request, request_id: str):
    """Create the account and email the requester.

    The account is made with a password nobody keeps, so the requester sets
    their own through "Forgot password" in the app. The notice says so; it
    carries no credential.
    """
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    try:
        outcome = await admin.approve_request(
            redis, operator, request_id, request.app.state.email_sender
        )
    except users.EmailTaken:
        context = await _requests_context(redis, operator)
        context["error"] = "That address already has an account."
        return _page(request, "requests", context, status_code=400)

    if outcome is None:
        # Approved twice, or rejected in another tab. Nothing to report.
        return RedirectResponse("/admin/ui/requests", status_code=303)

    account, sent = outcome
    flash_id = await _flash_put(redis, {"email": account["email"], "sent": sent})
    return RedirectResponse(f"/admin/ui/requests?flash={flash_id}", status_code=303)


@router.post("/requests/{request_id}/reject")
async def reject_request(request: Request, request_id: str):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    await admin.reject_request(request.app.state.redis, operator, request_id)
    return RedirectResponse("/admin/ui/requests", status_code=303)


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


# --- outbound email -------------------------------------------------------


async def _smtp_context(redis, operator: dict, candidate) -> dict:
    """Everything the email page needs, including what is actually in use."""
    stored = await smtp_settings.load(redis)
    resolved = await smtp_settings.resolve(redis, config.settings)

    if stored is not None:
        source = "panel"
    elif config.settings.email_sender == "smtp":
        source = "environment"
    else:
        source = "none"

    return {
        "admin": operator,
        "page": "smtp",
        # A plain dict without the password: the secret never enters the template
        # context, so no future edit to the page can render it by accident.
        "form": {
            "host": candidate.host,
            "port": candidate.port,
            "sender": candidate.sender,
            "username": candidate.username,
            "security": candidate.security,
        },
        "has_password": bool(candidate.password),
        "source": source,
        "active": resolved is not None,
        "active_host": resolved.host if resolved is not None else "",
        "error": None,
        "notice": None,
    }


def _smtp_page(request: Request, context: dict, status_code: int = 200):
    return _page(request, "smtp", context, status_code=status_code)


@router.get("/smtp", response_class=HTMLResponse)
async def smtp_page(request: Request, saved: int = 0):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    stored = await smtp_settings.load(redis)
    candidate = stored if stored is not None else smtp_settings.from_settings(config.settings)

    context = await _smtp_context(redis, operator, candidate)
    if saved:
        context["notice"] = "Saved. Password-reset codes use this server from now on."
    return _smtp_page(request, context)


def _submitted(
    stored, host: str, port: str, security: str, username: str, password: str, sender: str
) -> tuple[smtp_settings.SmtpConfig, str | None]:
    """Turn the form into a configuration, plus a complaint if the port is not a number.

    A bad port still yields a configuration, so the page can redraw with what was
    typed instead of wiping the form.
    """
    try:
        port_number = int(port)
        problem = None
    except ValueError:
        port_number = 587
        problem = "The port must be a number."

    return (
        smtp_settings.SmtpConfig(
            host=host.strip(),
            port=port_number,
            sender=sender.strip(),
            username=username.strip(),
            # Blank means "keep the stored one", so the form never has to carry
            # the secret back to the browser to be able to save again.
            password=password or (stored.password if stored is not None else ""),
            security=security,
        ),
        problem,
    )


@router.post("/smtp")
async def smtp_submit(
    request: Request,
    action: str = Form("save"),
    host: str = Form(""),
    port: str = Form("587"),
    security: str = Form("starttls"),
    username: str = Form(""),
    password: str = Form(""),
    sender: str = Form(""),
    test_to: str = Form(""),
):
    """Save the configuration, or send one test message through it.

    One form with two buttons rather than two forms: the test has to run against
    the values on screen, and duplicating eight fields is how the two drift.
    """
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    stored = await smtp_settings.load(redis)
    candidate, problem = _submitted(stored, host, port, security, username, password, sender)

    if problem is not None:
        context = await _smtp_context(redis, operator, candidate)
        context["error"] = problem
        return _smtp_page(request, context, status_code=400)

    if action == "test":
        return await _smtp_test(request, operator, candidate, test_to)

    problem = candidate.problem()
    if problem is not None:
        context = await _smtp_context(redis, operator, candidate)
        context["error"] = problem
        return _smtp_page(request, context, status_code=400)

    await smtp_settings.save(redis, candidate)
    await email.refresh(request.app)
    await admin.record(
        redis,
        operator,
        "smtp_saved",
        target=candidate.host,
        detail=f"{candidate.sender} via {candidate.security}",
    )
    return RedirectResponse("/admin/ui/smtp?saved=1", status_code=303)


async def _smtp_test(request: Request, operator: dict, candidate, test_to: str):
    """Send one message through the values on screen, without saving them."""
    redis = request.app.state.redis
    recipient = test_to.strip()

    if not _EMAIL_RE.match(recipient):
        context = await _smtp_context(redis, operator, candidate)
        context["error"] = "Enter an address to send the test to."
        return _smtp_page(request, context, status_code=400)

    problem = candidate.problem()
    if problem is not None:
        context = await _smtp_context(redis, operator, candidate)
        context["error"] = problem
        return _smtp_page(request, context, status_code=400)

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
            to=recipient,
            subject="DogWalk SMTP test",
            body=email.test_email_body(candidate.host),
        )
    except Exception as error:
        await admin.record(
            redis, operator, "smtp_test", target=recipient, detail=f"failed: {type(error).__name__}"
        )
        context = await _smtp_context(redis, operator, candidate)
        context["error"] = f"{type(error).__name__}: {error}"
        return _smtp_page(request, context, status_code=400)

    await admin.record(redis, operator, "smtp_test", target=recipient, detail="delivered")
    context = await _smtp_context(redis, operator, candidate)
    context["notice"] = f"Test message sent to {recipient} through {candidate.host}."
    return _smtp_page(request, context)


@router.post("/smtp/clear")
async def smtp_clear(request: Request):
    operator = await _current(request)
    if operator is None:
        return _to_login()

    redis = request.app.state.redis
    await smtp_settings.clear(redis)
    await email.refresh(request.app)
    await admin.record(redis, operator, "smtp_cleared")
    return RedirectResponse("/admin/ui/smtp", status_code=303)
