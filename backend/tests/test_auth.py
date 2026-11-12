import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth
from app.main import create_app

PASSWORD = "correct-horse-battery"
EMAIL = "alice@example.com"


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def client(redis_client):
    # Rate limiting off: these tests drive the per-email lockout, and the per-IP
    # limiter would answer first. test_ratelimit.py covers the limiter itself.
    with TestClient(create_app(redis_client=redis_client, rate_limit=False, invite_required=False)) as test_client:
        yield test_client


def register(client, email: str = EMAIL, password: str = PASSWORD) -> dict:
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_password_hash_round_trip():
    stored = auth.hash_password(PASSWORD)

    assert stored.startswith("scrypt$")
    assert PASSWORD not in stored
    assert auth.verify_password(PASSWORD, stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password(PASSWORD, "garbage")


def test_register_returns_a_usable_token_pair(client):
    body = register(client)

    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user_id"]
    assert body["access_token"] != body["refresh_token"]


def test_register_rejects_duplicate_email(client):
    register(client)

    response = client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 409


def test_register_rejects_short_password(client):
    response = client.post("/auth/register", json={"email": EMAIL, "password": "short"})

    assert response.status_code == 422


def test_register_rejects_malformed_email(client):
    response = client.post("/auth/register", json={"email": "not-an-email", "password": PASSWORD})

    assert response.status_code == 422


def test_login_returns_tokens(client):
    register(client)

    response = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_with_wrong_password_is_401(client):
    register(client)

    response = client.post("/auth/login", json={"email": EMAIL, "password": "wrong-password"})

    assert response.status_code == 401


def test_login_with_unknown_email_is_401(client):
    response = client.post("/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})

    assert response.status_code == 401


def test_login_locks_out_after_repeated_failures(client):
    register(client)

    for _ in range(10):
        assert client.post(
            "/auth/login", json={"email": EMAIL, "password": "wrong-password"}
        ).status_code == 401

    # The eleventh attempt is refused before the password is even checked.
    assert client.post(
        "/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 429


def test_refresh_rotates_and_the_old_token_stops_working(client):
    body = register(client)

    first = client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert first.status_code == 200
    rotated = first.json()
    assert rotated["refresh_token"] != body["refresh_token"]

    # Replaying the consumed token must fail.
    replay = client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert replay.status_code == 401

    # The rotated one still works.
    assert client.post(
        "/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    ).status_code == 200


def test_refresh_rejects_an_access_token(client):
    body = register(client)

    response = client.post("/auth/refresh", json={"refresh_token": body["access_token"]})

    assert response.status_code == 401


def test_logout_revokes_the_refresh_token(client):
    body = register(client)

    assert client.post("/auth/logout", json={"refresh_token": body["refresh_token"]}).status_code == 204
    assert client.post(
        "/auth/refresh", json={"refresh_token": body["refresh_token"]}
    ).status_code == 401


def test_logout_all_invalidates_existing_access_tokens(client):
    body = register(client)

    assert client.post("/auth/logout-all", headers=bearer(body["access_token"])).status_code == 204

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws/walk", headers=bearer(body["access_token"])) as ws:
            ws.receive_json()

    assert closed.value.code == 4401


def test_websocket_without_a_token_is_closed_4401(client):
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws/walk") as ws:
            ws.receive_json()

    assert closed.value.code == 4401


def test_websocket_with_a_garbage_token_is_closed_4401(client):
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws/walk", headers=bearer("not-a-jwt")) as ws:
            ws.receive_json()

    assert closed.value.code == 4401


def test_websocket_ignores_a_token_in_the_query_string(client):
    """Tokens belong in the header; a query-string token must not authenticate."""
    body = register(client)

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"/ws/walk?token={body['access_token']}") as ws:
            ws.receive_json()

    assert closed.value.code == 4401


def test_walker_id_comes_from_the_token_not_the_client(client):
    """The whole point of the change: a client cannot claim to be someone else."""
    alice = register(client, "alice@example.com")
    bob = register(client, "bob@example.com")

    # Bob presents his own token but tries to name himself as Alice.
    with client.websocket_connect(
        f"/ws/walk?walker_id={alice['user_id']}&name=Alice",
        headers=bearer(bob["access_token"]),
    ) as ws:
        welcome = ws.receive_json()

    assert welcome["walker_id"] == bob["user_id"]
    assert welcome["walker_id"] != alice["user_id"]
