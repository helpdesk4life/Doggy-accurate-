"""The HTML admin pages.

These are a skin over the same store functions the JSON API uses, so the tests
here are about the things a browser adds: the cookie, the redirects, and the
fact that a page cannot be reached without one.
"""

import asyncio
import re

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import admin
from app.admin_ui import COOKIE
from app.main import create_app

ADMIN_EMAIL = "operator@example.com"
ADMIN_PASSWORD = "operator-password"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def client(redis_client):
    with TestClient(
        create_app(redis_client=redis_client, rate_limit=False, invite_required=False)
    ) as test_client:
        yield test_client


@pytest.fixture
def operator(redis_client):
    return asyncio.run(admin.create_admin(redis_client, ADMIN_EMAIL, ADMIN_PASSWORD))


def sign_in(client) -> None:
    response = client.post(
        "/admin/ui/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def register(client, email: str = "alice@example.com") -> dict:
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


# --- the cookie -------------------------------------------------------------


def test_login_page_renders(client, operator):
    response = client.get("/admin/ui/login")

    assert response.status_code == 200
    assert "Sign in" in response.text


def test_pages_redirect_to_login_without_a_cookie(client, operator):
    for path in ("/admin/ui", "/admin/ui/users", "/admin/ui/invites", "/admin/ui/audit"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/admin/ui/login"


def test_login_sets_a_scoped_httponly_cookie(client, operator):
    response = client.post(
        "/admin/ui/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui"

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/admin" in cookie


def test_login_with_a_wrong_password_shows_the_error_and_sets_nothing(client, operator):
    response = client.post(
        "/admin/ui/login",
        data={"email": ADMIN_EMAIL, "password": "not-the-password"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "do not match" in response.text
    assert "set-cookie" not in response.headers


def test_a_walker_token_in_the_cookie_is_not_an_admin_session(client, operator):
    session = register(client)
    client.cookies.set("dogwalk_admin", session["access_token"])

    response = client.get("/admin/ui", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/login"


def test_logout_clears_the_cookie(client, operator):
    sign_in(client)

    response = client.post("/admin/ui/logout", follow_redirects=False)

    assert response.status_code == 303
    assert client.get("/admin/ui", follow_redirects=False).status_code == 303


def test_logout_revokes_the_token_server_side(client, operator):
    """Dropping the cookie is not enough: the token itself must stop working,
    or a stolen cookie stays valid until the token expires."""
    login = client.post(
        "/admin/ui/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    token = login.cookies.get(COOKIE)
    assert token
    assert client.get("/admin/ui", follow_redirects=False).status_code == 200

    client.post("/admin/ui/logout", follow_redirects=False)

    # The same token, presented again after the cookie was dropped, is refused.
    client.cookies.set(COOKIE, token)
    response = client.get("/admin/ui", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/login"


# --- pages ------------------------------------------------------------------


def test_dashboard_counts_accounts_and_invites(client, operator):
    register(client, "alice@example.com")
    register(client, "bob@example.com")
    sign_in(client)

    response = client.get("/admin/ui")

    assert response.status_code == 200
    assert ">2<" in response.text
    assert "accounts" in response.text


def test_users_page_lists_accounts_newest_first(client, operator):
    register(client, "alice@example.com")
    register(client, "bob@example.com")
    sign_in(client)

    body = client.get("/admin/ui/users").text

    assert body.index("bob@example.com") < body.index("alice@example.com")


def test_revoke_from_the_page_kills_the_session(client, operator):
    session = register(client)
    sign_in(client)

    response = client.post(
        f"/admin/ui/users/{session['user_id']}/revoke",
        data={"offset": "0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        client.post("/auth/logout-all", headers={"Authorization": f"Bearer {session['access_token']}"}).status_code
        == 401
    )


def test_delete_goes_through_a_confirmation_page(client, operator):
    session = register(client)
    sign_in(client)

    confirm = client.get(f"/admin/ui/users/{session['user_id']}/delete")
    assert confirm.status_code == 200
    assert "alice@example.com" in confirm.text
    assert "cannot be undone" in confirm.text

    response = client.post(
        f"/admin/ui/users/{session['user_id']}/delete",
        data={"offset": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert client.get("/admin/ui/users").text.count("alice@example.com") == 0


def test_mint_shows_the_codes_once(client, operator):
    sign_in(client)

    response = client.post(
        "/admin/ui/invites", data={"count": "2"}, follow_redirects=False
    )
    assert response.status_code == 303

    location = response.headers["location"]
    assert location.startswith("/admin/ui/invites?flash=")

    first = client.get(location).text
    assert first.count("-") >= 2
    assert "New codes" in first

    # The flash is consumed, so a refresh does not show them again.
    second = client.get(location).text
    assert "New codes" not in second


def test_audit_page_lists_actions(client, operator):
    sign_in(client)
    client.post("/admin/ui/invites", data={"count": "1"}, follow_redirects=False)

    body = client.get("/admin/ui/audit").text

    assert "mint_invites" in body
    assert ADMIN_EMAIL in body


# --- creating accounts ------------------------------------------------------


def created_password(body: str) -> str:
    """Pull the handed-over password out of the one-shot notice."""
    match = re.search(r'<p class="mono">[^<]*·\s*([A-Z0-9]{16})</p>', body)
    assert match, body
    return match.group(1)


def test_the_accounts_page_carries_the_form(client, operator):
    sign_in(client)

    body = client.get("/admin/ui/users").text

    assert "Add an account" in body
    assert 'action="/admin/ui/users"' in body
    # The password is generated, so there is nothing to type but the address.
    assert 'name="password"' not in body


def test_create_account_shows_the_password_once(client, operator):
    sign_in(client)

    response = client.post(
        "/admin/ui/users", data={"email": "dave@example.com"}, follow_redirects=False
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/admin/ui/users?flash=")

    first = client.get(location).text
    assert "Account created" in first
    assert "dave@example.com" in first
    password = created_password(first)

    # The flash is consumed, so a refresh does not show it again.
    second = client.get(location).text
    assert "Account created" not in second
    assert password not in second


def test_the_shown_password_opens_the_change_flow(client, operator):
    sign_in(client)
    response = client.post(
        "/admin/ui/users", data={"email": "dave@example.com"}, follow_redirects=False
    )
    password = created_password(client.get(response.headers["location"]).text)

    login = client.post(
        "/auth/login", json={"email": "dave@example.com", "password": password}
    )

    assert login.status_code == 200, login.text
    body = login.json()
    assert body["must_change_password"] is True
    assert body["change_token"]
    assert body["access_token"] is None


def test_create_account_with_a_duplicate_address_shows_the_error(client, operator):
    register(client, "alice@example.com")
    sign_in(client)

    response = client.post(
        "/admin/ui/users",
        data={"email": "alice@example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "already has an account" in response.text
    # The address is echoed back so the operator does not retype it...
    assert 'value="alice@example.com"' in response.text
    # ...and nothing was created.
    assert "1 registered" in response.text


def test_create_account_with_a_bad_address_shows_the_error(client, operator):
    sign_in(client)

    response = client.post(
        "/admin/ui/users",
        data={"email": "not-an-address"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "does not look like an email address" in response.text
    assert "0 registered" in response.text


def test_create_account_needs_a_cookie(client, operator):
    response = client.post(
        "/admin/ui/users", data={"email": "dave@example.com"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/login"

    sign_in(client)
    assert "0 registered" in client.get("/admin/ui/users").text


def test_the_password_never_reaches_the_audit_page(client, operator):
    sign_in(client)
    response = client.post(
        "/admin/ui/users", data={"email": "dave@example.com"}, follow_redirects=False
    )
    password = created_password(client.get(response.headers["location"]).text)

    audit = client.get("/admin/ui/audit").text
    assert "create_user" in audit
    assert password not in audit


def test_no_page_carries_a_position(client, operator):
    """Same promise as the JSON API: the admin surface never shows a walker."""
    register(client)
    sign_in(client)

    for path in ("/admin/ui", "/admin/ui/users", "/admin/ui/invites", "/admin/ui/audit"):
        body = client.get(path).text.lower()
        for word in ("latitude", "longitude", "bearing", "distance_m", "accuracy"):
            assert word not in body, f"{path} mentions {word}"


# --- the operator's own password --------------------------------------------


def change_password(client, current: str, new: str, confirm: str | None = None):
    return client.post(
        "/admin/ui/password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirm is None else confirm,
        },
        follow_redirects=False,
    )


def test_the_password_page_renders(client, operator):
    sign_in(client)

    response = client.get("/admin/ui/password")

    assert response.status_code == 200
    assert "Current password" in response.text


def test_the_password_page_needs_a_cookie(client, operator):
    response = client.get("/admin/ui/password", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/login"


def test_changing_the_password_keeps_this_browser_signed_in(client, operator):
    sign_in(client)

    response = change_password(client, ADMIN_PASSWORD, "a-brand-new-password")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/password?saved=1"
    assert client.get("/admin/ui", follow_redirects=False).status_code == 200


def test_changing_the_password_signs_out_another_browser(client, operator):
    sign_in(client)
    other_browser = client.cookies.get("dogwalk_admin")

    change_password(client, ADMIN_PASSWORD, "a-brand-new-password")

    client.cookies.clear()
    client.cookies.set("dogwalk_admin", other_browser)
    response = client.get("/admin/ui", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/login"


def test_the_new_password_opens_the_login_page(client, operator):
    sign_in(client)
    change_password(client, ADMIN_PASSWORD, "a-brand-new-password")
    client.post("/admin/ui/logout", follow_redirects=False)

    response = client.post(
        "/admin/ui/login",
        data={"email": ADMIN_EMAIL, "password": "a-brand-new-password"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_a_mismatched_confirmation_is_refused(client, operator):
    sign_in(client)

    response = change_password(
        client, ADMIN_PASSWORD, "a-brand-new-password", confirm="something-else"
    )

    assert response.status_code == 400
    assert "do not match" in response.text


def test_a_wrong_current_password_is_refused(client, operator):
    sign_in(client)

    response = change_password(client, "not-the-password", "a-brand-new-password")

    assert response.status_code == 400
    assert "not your current password" in response.text


def test_the_same_password_is_refused(client, operator):
    sign_in(client)

    response = change_password(client, ADMIN_PASSWORD, ADMIN_PASSWORD)

    assert response.status_code == 400
    assert "different" in response.text


def test_a_short_new_password_is_refused(client, operator):
    sign_in(client)

    response = change_password(client, ADMIN_PASSWORD, "short")

    assert response.status_code == 400
    assert "at least 8" in response.text
