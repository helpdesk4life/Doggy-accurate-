"""Per-IP throttling on the auth endpoints.

The per-email lockout in ``users.py`` stops one account being brute forced. These
tests cover the other half: credential stuffing across many accounts, and
``/auth/register`` being hammered to fill Redis.

Every request here uses a fresh email, so the per-email lockout can never be the
thing that answers. A limiter 429 carries ``Retry-After``; the lockout 429 does
not, which is how the two are told apart.
"""

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import ratelimit
from app.main import create_app

PASSWORD = "correct-horse-battery"


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def client(redis_client):
    with TestClient(create_app(redis_client=redis_client, rate_limit=True, invite_required=False)) as test_client:
        yield test_client


def login(client, index: int, address: str | None = None):
    headers = {"X-Forwarded-For": address} if address else {}
    return client.post(
        "/auth/login",
        json={"email": f"user{index}@example.com", "password": PASSWORD},
        headers=headers,
    )


def test_login_is_throttled_per_address(client, frozen_window):
    limit = ratelimit.LIMITS["/auth/login"]

    for index in range(limit.requests):
        assert login(client, index).status_code == 401

    blocked = login(client, limit.requests)

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_register_is_throttled(client, frozen_window):
    limit = ratelimit.LIMITS["/auth/register"]

    for index in range(limit.requests):
        response = client.post(
            "/auth/register",
            json={"email": f"user{index}@example.com", "password": PASSWORD},
        )
        assert response.status_code == 201

    blocked = client.post(
        "/auth/register",
        json={"email": "one-too-many@example.com", "password": PASSWORD},
    )

    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers


def test_throttling_is_per_address_not_global(client):
    """One noisy client must not lock everyone else out."""
    limit = ratelimit.LIMITS["/auth/login"]

    for index in range(limit.requests + 1):
        login(client, index, address="203.0.113.9")

    other = login(client, 999, address="198.51.100.4")

    assert other.status_code == 401


def test_health_is_not_throttled(client):
    for _ in range(50):
        assert client.get("/health").status_code == 200


def test_the_limiter_can_be_switched_off(redis_client):
    """The auth tests need the per-email lockout to answer first."""
    with TestClient(create_app(redis_client=redis_client, rate_limit=False, invite_required=False)) as client:
        limit = ratelimit.LIMITS["/auth/login"]
        for index in range(limit.requests + 5):
            assert login(client, index).status_code == 401


def test_a_window_boundary_resets_the_counter(client, frozen_window):
    """Pins the fixed-window tradeoff that the tests above have to work around.

    A burst that straddles the boundary gets a second window's worth. That is why
    the limiter is described as blunting automated abuse rather than metering
    traffic, and why every test that counts requests pins the clock.
    """
    limit = ratelimit.LIMITS["/auth/login"]

    for index in range(limit.requests):
        assert login(client, index).status_code == 401
    assert login(client, limit.requests).status_code == 429

    frozen_window.advance(limit.window_seconds)

    assert login(client, limit.requests + 1).status_code == 401
