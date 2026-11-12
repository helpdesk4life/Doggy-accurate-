"""The admin API: separate identity, account management, invite codes.

The property worth protecting is the boundary, not the CRUD. An admin token must
not open a walk socket, a walker token must not open the admin API, and no admin
response may carry a position. Everything else here is bookkeeping.
"""

import asyncio
import json
import re

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import admin, ratelimit
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
    """An admin account, created the way an operator creates one: directly."""
    return asyncio.run(admin.create_admin(redis_client, ADMIN_EMAIL, ADMIN_PASSWORD))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register(client, email: str = "alice@example.com") -> dict:
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def admin_token(client, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD) -> str:
    response = client.post("/admin/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _keys(value) -> set[str]:
    """Every key name anywhere in a JSON body."""
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= _keys(item)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= _keys(item)
        return found
    return set()


# --- the boundary -----------------------------------------------------------


def test_admin_login_returns_a_token(client, operator):
    response = client.post(
        "/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["email"] == ADMIN_EMAIL
    assert body["admin_id"] == operator["admin_id"]


def test_admin_login_issues_no_refresh_token(client, operator):
    """An operator signs in again when the token expires; there is no rotation."""
    body = client.post(
        "/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    ).json()

    assert "refresh_token" not in body


def test_admin_login_rejects_a_wrong_password(client, operator):
    response = client.post(
        "/admin/auth/login", json={"email": ADMIN_EMAIL, "password": "not-the-password"}
    )

    assert response.status_code == 401


def test_admin_login_rejects_an_unknown_address(client, operator):
    response = client.post(
        "/admin/auth/login", json={"email": "nobody@example.com", "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 401


def test_admin_login_locks_out_after_repeated_failures(client, operator):
    for _ in range(admin.MAX_LOGIN_FAILURES):
        assert (
            client.post(
                "/admin/auth/login",
                json={"email": ADMIN_EMAIL, "password": "not-the-password"},
            ).status_code
            == 401
        )

    response = client.post(
        "/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 429


def test_admin_routes_require_a_token(client, operator):
    for method, path in [
        ("get", "/admin/users"),
        ("get", "/admin/users/abc"),
        ("post", "/admin/users/abc/revoke-sessions"),
        ("delete", "/admin/users/abc"),
        ("get", "/admin/invites"),
        ("post", "/admin/invites"),
        ("get", "/admin/stats"),
        ("get", "/admin/audit"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{method.upper()} {path}"


def test_a_walker_token_cannot_open_the_admin_api(client, operator):
    """The whole reason admin tokens are a separate type rather than a role claim."""
    session = register(client)

    response = client.get("/admin/users", headers=bearer(session["access_token"]))

    assert response.status_code == 401


def test_an_admin_token_cannot_open_a_walk_socket(client, operator):
    token = admin_token(client)

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws/walk", headers=bearer(token)) as ws:
            ws.receive_json()

    assert closed.value.code == 4401


def test_an_admin_token_is_not_a_walker_token(client, operator):
    token = admin_token(client)

    assert client.post("/auth/logout-all", headers=bearer(token)).status_code == 401


def test_no_admin_response_carries_a_position(client, operator):
    """The product promise: coordinates never leave the server, admin or not."""
    session = register(client)
    headers = bearer(admin_token(client))
    client.post("/admin/invites", json={"count": 1}, headers=headers)

    bodies = [
        client.get("/admin/users", headers=headers).json(),
        client.get(f"/admin/users/{session['user_id']}", headers=headers).json(),
        client.get("/admin/invites", headers=headers).json(),
        client.get("/admin/stats", headers=headers).json(),
        client.get("/admin/audit", headers=headers).json(),
    ]

    forbidden = {
        "lat",
        "lon",
        "latitude",
        "longitude",
        "distance_m",
        "bearing_deg",
        "position",
        "accuracy",
    }
    for body in bodies:
        assert not forbidden & _keys(body)


# --- accounts ---------------------------------------------------------------


def test_list_users_is_newest_first(client, operator):
    register(client, "alice@example.com")
    register(client, "bob@example.com")
    register(client, "carol@example.com")

    body = client.get("/admin/users", headers=bearer(admin_token(client))).json()

    assert body["total"] == 3
    assert [user["email"] for user in body["users"]] == [
        "carol@example.com",
        "bob@example.com",
        "alice@example.com",
    ]


def test_list_users_pages(client, operator):
    for name in ("alice", "bob", "carol"):
        register(client, f"{name}@example.com")
    headers = bearer(admin_token(client))

    first = client.get("/admin/users?offset=0&limit=2", headers=headers).json()
    second = client.get("/admin/users?offset=2&limit=2", headers=headers).json()

    assert [user["email"] for user in first["users"]] == ["carol@example.com", "bob@example.com"]
    assert [user["email"] for user in second["users"]] == ["alice@example.com"]
    assert first["total"] == second["total"] == 3


def test_list_users_rejects_an_oversized_page(client, operator):
    response = client.get("/admin/users?limit=1000", headers=bearer(admin_token(client)))

    assert response.status_code == 422


def test_user_detail_reports_sessions(client, operator):
    session = register(client)

    body = client.get(
        f"/admin/users/{session['user_id']}", headers=bearer(admin_token(client))
    ).json()

    assert body["email"] == "alice@example.com"
    assert body["sessions"] == 1
    assert body["token_version"] == 1


def test_unknown_user_is_404(client, operator):
    headers = bearer(admin_token(client))

    assert client.get("/admin/users/deadbeef", headers=headers).status_code == 404
    assert client.delete("/admin/users/deadbeef", headers=headers).status_code == 404
    assert (
        client.post("/admin/users/deadbeef/revoke-sessions", headers=headers).status_code == 404
    )


def test_revoke_sessions_invalidates_every_token(client, operator):
    session = register(client)
    headers = bearer(admin_token(client))

    response = client.post(f"/admin/users/{session['user_id']}/revoke-sessions", headers=headers)
    assert response.status_code == 204

    # The access token is dead...
    assert (
        client.post("/auth/logout-all", headers=bearer(session["access_token"])).status_code == 401
    )
    # ...and so is the refresh token, so the client cannot mint a new one.
    assert (
        client.post("/auth/refresh", json={"refresh_token": session["refresh_token"]}).status_code
        == 401
    )


def test_delete_user_removes_the_account_and_the_index_entry(client, operator):
    session = register(client)
    headers = bearer(admin_token(client))

    assert client.delete(f"/admin/users/{session['user_id']}", headers=headers).status_code == 204

    assert (
        client.post(
            "/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert client.get("/admin/users", headers=headers).json()["total"] == 0


def create_account(client, email: str = "dave@example.com") -> dict:
    """Create an account the way an operator does, and return the response."""
    response = client.post(
        "/admin/users", json={"email": email}, headers=bearer(admin_token(client))
    )
    assert response.status_code == 201, response.text
    return response.json()


def change_ticket(client, created: dict) -> str:
    """Sign in with the handed-over password and take the ticket it returns."""
    response = client.post(
        "/auth/login",
        json={"email": created["email"], "password": created["temporary_password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["change_token"]


def test_create_user_returns_a_temporary_password(client, operator):
    response = client.post(
        "/admin/users",
        json={"email": "dave@example.com"},
        headers=bearer(admin_token(client)),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "dave@example.com"
    assert body["user_id"]
    assert len(body["temporary_password"]) == 16
    # The hash users.create_user returns must not travel back out.
    assert "password_hash" not in response.text


def test_a_created_account_cannot_sign_in_normally(client, operator):
    """The handed-over password buys a password change, not a session."""
    created = create_account(client)

    response = client.post(
        "/auth/login",
        json={"email": "dave@example.com", "password": created["temporary_password"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["must_change_password"] is True
    assert body["change_token"]
    # Absent rather than empty, so a client that ignores the flag fails loudly.
    assert body["access_token"] is None
    assert body["refresh_token"] is None


def test_the_change_ticket_opens_nothing_else(client, operator):
    ticket = change_ticket(client, create_account(client))

    assert client.post("/auth/logout-all", headers=bearer(ticket)).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": ticket}).status_code == 401
    assert client.get("/admin/users", headers=bearer(ticket)).status_code == 401


def test_changing_the_password_signs_the_account_in(client, operator):
    ticket = change_ticket(client, create_account(client))

    response = client.post(
        "/auth/password/change",
        json={"new_password": "daves-own-password"},
        headers=bearer(ticket),
    )

    assert response.status_code == 200, response.text
    session = response.json()
    assert session["access_token"] and session["refresh_token"]
    # The pair it returns works straight away, so there is no second sign-in.
    assert (
        client.post("/auth/logout-all", headers=bearer(session["access_token"])).status_code
        == 204
    )


def test_the_temporary_password_stops_working(client, operator):
    created = create_account(client)
    ticket = change_ticket(client, created)
    client.post(
        "/auth/password/change",
        json={"new_password": "daves-own-password"},
        headers=bearer(ticket),
    )

    assert (
        client.post(
            "/auth/login",
            json={"email": "dave@example.com", "password": created["temporary_password"]},
        ).status_code
        == 401
    )

    # ...and the new one is an ordinary sign-in, not another ticket.
    body = client.post(
        "/auth/login", json={"email": "dave@example.com", "password": "daves-own-password"}
    ).json()
    assert body["must_change_password"] is False
    assert body["access_token"]


def test_the_change_ticket_is_spent_by_the_change(client, operator):
    ticket = change_ticket(client, create_account(client))
    client.post(
        "/auth/password/change", json={"new_password": "daves-own-password"}, headers=bearer(ticket)
    )

    # token_version moved, so the ticket that authorised the change is dead too.
    assert (
        client.post(
            "/auth/password/change",
            json={"new_password": "another-password"},
            headers=bearer(ticket),
        ).status_code
        == 401
    )


def test_changing_to_the_temporary_password_is_refused(client, operator):
    created = create_account(client)
    ticket = change_ticket(client, created)

    response = client.post(
        "/auth/password/change",
        json={"new_password": created["temporary_password"]},
        headers=bearer(ticket),
    )

    # Accepting it back would clear the flag without changing anything.
    assert response.status_code == 400
    assert "different" in response.json()["detail"]


def test_changing_the_password_needs_a_change_ticket(client, operator):
    payload = {"new_password": "whatever-password"}

    assert client.post("/auth/password/change", json=payload).status_code == 401

    # An ordinary access token is not a ticket: different typ, refused on the
    # type check alone.
    walker = register(client)["access_token"]
    assert client.post("/auth/password/change", json=payload, headers=bearer(walker)).status_code == 401


def test_a_short_new_password_is_rejected(client, operator):
    ticket = change_ticket(client, create_account(client))

    response = client.post(
        "/auth/password/change", json={"new_password": "short"}, headers=bearer(ticket)
    )

    assert response.status_code == 422


def test_create_user_rejects_a_duplicate_address(client, operator):
    headers = bearer(admin_token(client))
    register(client, "alice@example.com")

    response = client.post("/admin/users", json={"email": "Alice@Example.com"}, headers=headers)

    assert response.status_code == 409
    assert client.get("/admin/users", headers=headers).json()["total"] == 1


def test_create_user_needs_an_admin_token(client, operator):
    payload = {"email": "dave@example.com"}

    # 401 rather than 422: the dependency resolves before the body is validated,
    # so an anonymous caller learns nothing about the payload's shape.
    assert client.post("/admin/users", json=payload).status_code == 401

    walker = register(client)["access_token"]
    assert client.post("/admin/users", json=payload, headers=bearer(walker)).status_code == 401


def test_create_user_spends_no_invite_code(client, operator):
    headers = bearer(admin_token(client))
    client.post("/admin/invites", json={"count": 2}, headers=headers)

    client.post("/admin/users", json={"email": "dave@example.com"}, headers=headers)

    # The code gates strangers registering themselves. An operator is not one.
    assert client.get("/admin/invites", headers=headers).json()["unused"] == 2


def test_audit_records_a_creation_without_the_password(client, operator):
    headers = bearer(admin_token(client))
    created = create_account(client)

    entry = client.get("/admin/audit", headers=headers).json()["entries"][0]
    assert entry["action"] == "create_user"
    assert entry["target"] == created["user_id"]
    assert entry["detail"] == "dave@example.com"
    assert created["temporary_password"] not in json.dumps(entry)


# --- invites ----------------------------------------------------------------


def test_mint_and_list_invites(client, operator):
    headers = bearer(admin_token(client))

    minted = client.post("/admin/invites", json={"count": 3}, headers=headers)
    assert minted.status_code == 201
    codes = minted.json()["codes"]
    assert len(codes) == 3
    assert all(re.fullmatch(r"[A-Z2-9]{5}-[A-Z2-9]{5}", code) for code in codes)

    listed = client.get("/admin/invites", headers=headers).json()
    assert listed["unused"] == 3
    assert listed["used"] == 0
    assert sorted(listed["codes"]) == sorted(codes)


def test_minted_codes_actually_register(redis_client, operator):
    """The admin mint is the same code path the CLI uses, so it must work."""
    with TestClient(
        create_app(redis_client=redis_client, rate_limit=False, invite_required=True)
    ) as gated:
        headers = bearer(admin_token(gated))
        code = gated.post("/admin/invites", json={"count": 1}, headers=headers).json()["codes"][0]

        response = gated.post(
            "/auth/register",
            json={"email": "friend@example.com", "password": PASSWORD, "invite_code": code},
        )

        assert response.status_code == 201
        assert gated.get("/admin/invites", headers=headers).json()["used"] == 1


def test_mint_rejects_a_zero_count(client, operator):
    response = client.post("/admin/invites", json={"count": 0}, headers=bearer(admin_token(client)))

    assert response.status_code == 422


def test_stats_counts_accounts_and_invites(client, operator):
    register(client, "alice@example.com")
    register(client, "bob@example.com")
    headers = bearer(admin_token(client))
    client.post("/admin/invites", json={"count": 2}, headers=headers)

    body = client.get("/admin/stats", headers=headers).json()

    assert body == {"accounts": 2, "invites_unused": 2, "invites_used": 0}


# --- audit ------------------------------------------------------------------


def test_audit_records_actions_but_not_the_codes(client, operator):
    headers = bearer(admin_token(client))
    codes = client.post("/admin/invites", json={"count": 3}, headers=headers).json()["codes"]

    entries = client.get("/admin/audit", headers=headers).json()["entries"]

    assert entries[0]["action"] == "mint_invites"
    assert entries[0]["admin_email"] == ADMIN_EMAIL
    assert entries[0]["detail"] == "3 code(s)"

    # The log is readable by every admin, so it must not be a second copy of
    # the secrets it is auditing.
    dumped = json.dumps(entries)
    for code in codes:
        assert code not in dumped
        assert code.replace("-", "") not in dumped


def test_audit_records_a_deletion_with_the_address(client, operator):
    session = register(client)
    headers = bearer(admin_token(client))

    client.delete(f"/admin/users/{session['user_id']}", headers=headers)

    entry = client.get("/admin/audit", headers=headers).json()["entries"][0]
    assert entry["action"] == "delete_user"
    assert entry["target"] == session["user_id"]
    assert entry["detail"] == "alice@example.com"


def test_audit_is_newest_first(client, operator):
    headers = bearer(admin_token(client))
    client.post("/admin/invites", json={"count": 1}, headers=headers)
    session = register(client)
    client.post(f"/admin/users/{session['user_id']}/revoke-sessions", headers=headers)

    actions = [
        entry["action"]
        for entry in client.get("/admin/audit", headers=headers).json()["entries"]
    ]

    assert actions[:3] == ["revoke_sessions", "mint_invites", "login"]


# --- throttling -------------------------------------------------------------


@pytest.fixture
def limited_client(redis_client):
    with TestClient(
        create_app(redis_client=redis_client, rate_limit=True, invite_required=False)
    ) as test_client:
        yield test_client


def test_admin_login_is_rate_limited(limited_client, operator):
    """The limiter answers with Retry-After; the per-email lockout does not."""
    limit = ratelimit.LIMITS["/admin/auth/login"]

    for _ in range(limit.requests):
        limited_client.post(
            "/admin/auth/login", json={"email": ADMIN_EMAIL, "password": "not-the-password"}
        )

    response = limited_client.post(
        "/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 429
    assert "Retry-After" in response.headers
