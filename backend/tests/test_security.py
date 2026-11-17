"""Password reset, account deletion, refresh replay and the startup checks."""

import asyncio
import re
import time

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import auth, config, email, users
from app.main import create_app

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-brand-new-password"
EMAIL = "alice@example.com"


class RecordingSender:
    """Stands in for the email sender so the reset code can be read back."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})

    def last_code(self) -> str:
        match = re.search(r"\b(\d{6})\b", self.sent[-1]["body"])
        assert match is not None, self.sent[-1]["body"]
        return match.group(1)


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def client(redis_client):
    with TestClient(create_app(redis_client=redis_client, rate_limit=False, invite_required=False)) as test_client:
        yield test_client


@pytest.fixture
def sender(client):
    recorder = RecordingSender()
    client.app.state.email_sender = recorder
    return recorder


def register(client, email: str = EMAIL, password: str = PASSWORD) -> dict:
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- password reset -------------------------------------------------------


def test_reset_request_sends_a_code(client, sender):
    register(client)

    response = client.post("/auth/password-reset/request", json={"email": EMAIL})

    assert response.status_code == 202
    assert len(sender.sent) == 1
    assert sender.sent[0]["to"] == EMAIL
    assert re.fullmatch(r"\d{6}", sender.last_code())


def test_reset_request_for_an_unknown_address_looks_identical(client, sender):
    """A different status here would be an account enumeration oracle."""
    response = client.post("/auth/password-reset/request", json={"email": "nobody@example.com"})

    assert response.status_code == 202
    assert sender.sent == []


def test_a_failing_sender_does_not_reveal_that_the_address_exists(client, sender):
    """A provider outage must not become an oracle either.

    If the send raised, a known address would answer 500 and an unknown one 202,
    which is the same leak the identical-status rule above exists to prevent.
    """
    register(client)

    async def explode(**kwargs):
        raise RuntimeError("provider is down")

    sender.send = explode

    response = client.post("/auth/password-reset/request", json={"email": EMAIL})

    assert response.status_code == 202


def test_reset_code_changes_the_password_and_kills_old_sessions(client, sender):
    session = register(client)
    client.post("/auth/password-reset/request", json={"email": EMAIL})
    code = sender.last_code()

    confirm = client.post(
        "/auth/password-reset/confirm",
        json={"email": EMAIL, "code": code, "password": NEW_PASSWORD},
    )
    assert confirm.status_code == 204

    # The old password is gone.
    assert client.post(
        "/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 401

    # The new one works.
    assert client.post(
        "/auth/login", json={"email": EMAIL, "password": NEW_PASSWORD}
    ).status_code == 200

    # Every token issued before the reset is dead.
    assert client.post(
        "/auth/refresh", json={"refresh_token": session["refresh_token"]}
    ).status_code == 401


def test_reset_code_is_single_use(client, sender):
    register(client)
    client.post("/auth/password-reset/request", json={"email": EMAIL})
    code = sender.last_code()

    body = {"email": EMAIL, "code": code, "password": NEW_PASSWORD}
    assert client.post("/auth/password-reset/confirm", json=body).status_code == 204
    assert client.post("/auth/password-reset/confirm", json=body).status_code == 400


def test_reset_rejects_a_wrong_code(client, sender):
    register(client)
    client.post("/auth/password-reset/request", json={"email": EMAIL})
    code = sender.last_code()
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"

    response = client.post(
        "/auth/password-reset/confirm",
        json={"email": EMAIL, "code": wrong, "password": NEW_PASSWORD},
    )

    assert response.status_code == 400


def test_reset_stops_accepting_codes_after_repeated_guesses(client, sender):
    register(client)
    client.post("/auth/password-reset/request", json={"email": EMAIL})
    code = sender.last_code()

    for _ in range(users.RESET_MAX_ATTEMPTS):
        client.post(
            "/auth/password-reset/confirm",
            json={"email": EMAIL, "code": "000000", "password": NEW_PASSWORD},
        )

    # Even the real code is refused now: the address is out of the flow.
    response = client.post(
        "/auth/password-reset/confirm",
        json={"email": EMAIL, "code": code, "password": NEW_PASSWORD},
    )

    assert response.status_code == 400


# --- account deletion -----------------------------------------------------


def test_delete_account_requires_the_password(client):
    session = register(client)

    response = client.request(
        "DELETE",
        "/auth/account",
        json={"password": "not-the-password"},
        headers=bearer(session["access_token"]),
    )

    assert response.status_code == 401
    # Still there.
    assert client.post(
        "/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 200


def test_delete_account_removes_the_account_and_its_tokens(client):
    session = register(client)

    response = client.request(
        "DELETE",
        "/auth/account",
        json={"password": PASSWORD},
        headers=bearer(session["access_token"]),
    )
    assert response.status_code == 204

    assert client.post(
        "/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 401
    assert client.post(
        "/auth/refresh", json={"refresh_token": session["refresh_token"]}
    ).status_code == 401

    # The email index is gone too, so the address can be registered again.
    assert client.post(
        "/auth/register", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 201


def test_delete_account_needs_a_token(client):
    register(client)

    response = client.request("DELETE", "/auth/account", json={"password": PASSWORD})

    assert response.status_code == 401


# --- refresh replay -------------------------------------------------------


def test_replay_inside_the_grace_window_does_not_revoke_the_account(client):
    """A client that loses the response to a refresh retries with the same token."""
    session = register(client)
    rotated = client.post(
        "/auth/refresh", json={"refresh_token": session["refresh_token"]}
    ).json()

    replay = client.post("/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert replay.status_code == 401

    # The rotated token still works: the account was not punished for a retry.
    assert client.post(
        "/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    ).status_code == 200


def test_replay_after_the_grace_window_revokes_every_session(client, redis_client):
    session = register(client)
    rotated = client.post(
        "/auth/refresh", json={"refresh_token": session["refresh_token"]}
    ).json()

    # Backdate the consumption marker past the grace window.
    from app import auth

    claims = auth.decode_token(session["refresh_token"], auth.REFRESH)
    key = users.USED_REFRESH_KEY.format(jti=claims.jti)
    stale = int(time.time()) - users.REPLAY_GRACE_SECONDS - 60
    asyncio.run(redis_client.set(key, f"{session['user_id']}|{stale}"))

    assert client.post(
        "/auth/refresh", json={"refresh_token": session["refresh_token"]}
    ).status_code == 401

    # The token the attacker did not get is dead as well.
    assert client.post(
        "/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    ).status_code == 401


class _YieldingRedis:
    """Wraps a redis client so every awaited command yields to the event loop
    first, creating the interleaving between concurrent callers that a plain
    in-memory call never produces."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if asyncio.iscoroutinefunction(attr):
            async def wrapper(*args, **kwargs):
                await asyncio.sleep(0)
                return await attr(*args, **kwargs)

            return wrapper
        return attr


def test_concurrent_refresh_of_the_same_token_is_consumed_once(redis_client):
    """Two parties presenting the same refresh token concurrently: exactly one
    exchange may succeed. With a separate GET-then-DELETE both could read the
    id before either deleted it; GETDEL makes the read-and-burn one command."""

    async def scenario():
        user = await users.create_user(redis_client, EMAIL, PASSWORD)
        pair = auth.issue_tokens(user["user_id"], 1)
        await users.store_refresh(redis_client, pair.refresh_jti, user["user_id"], 3600)
        racing = _YieldingRedis(redis_client)
        results = await asyncio.gather(
            users.consume_refresh(racing, pair.refresh_jti),
            users.consume_refresh(racing, pair.refresh_jti),
            users.consume_refresh(racing, pair.refresh_jti),
        )
        return user["user_id"], results

    user_id, results = asyncio.run(scenario())
    assert results.count(user_id) == 1
    assert results.count(None) == 2


# --- startup checks -------------------------------------------------------


def test_production_refuses_the_public_development_secret():
    settings = config.Settings(environment="production", jwt_secret=config.DEV_JWT_SECRET)

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        settings.validate()


def test_production_accepts_a_real_secret():
    settings = config.Settings(
        environment="production",
        jwt_secret="a" * 48,
        public_base_url="https://backend.example.com",
        # The email guard is a separate check; see the tests below.
        allow_logging_email=True,
    )

    settings.validate()


def test_production_refuses_to_log_reset_codes():
    settings = config.Settings(
        environment="production",
        jwt_secret="a" * 48,
        public_base_url="https://backend.example.com",
    )

    with pytest.raises(RuntimeError, match="EMAIL_SENDER"):
        settings.validate()


def test_production_accepts_an_acknowledged_logging_sender():
    settings = config.Settings(
        environment="production",
        jwt_secret="a" * 48,
        public_base_url="https://backend.example.com",
        allow_logging_email=True,
    )

    settings.validate()


def test_production_refuses_a_plain_http_base_url():
    settings = config.Settings(
        environment="production",
        jwt_secret="a" * 48,
        public_base_url="http://backend.example.com",
        allow_logging_email=True,
    )

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        settings.validate()


def test_production_refuses_a_localhost_base_url():
    settings = config.Settings(
        environment="production",
        jwt_secret="a" * 48,
        public_base_url="https://localhost:8000",
        allow_logging_email=True,
    )

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        settings.validate()


def test_development_tolerates_the_default_base_url():
    config.Settings(environment="development", jwt_secret=config.DEV_JWT_SECRET).validate()


def test_development_tolerates_the_logging_sender():
    config.Settings(
        environment="development",
        jwt_secret=config.DEV_JWT_SECRET,
    ).validate()


def test_an_unknown_sender_is_refused_at_startup():
    with pytest.raises(RuntimeError, match="Unknown EMAIL_SENDER"):
        email.build_sender("carrier-pigeon")


def test_the_logging_sender_is_what_the_default_name_builds():
    assert isinstance(email.build_sender("logging"), email.LoggingEmailSender)


def test_development_tolerates_the_public_secret():
    config.Settings(environment="development", jwt_secret=config.DEV_JWT_SECRET).validate()


def test_a_short_secret_is_refused_everywhere():
    settings = config.Settings(environment="production", jwt_secret="too-short")

    with pytest.raises(RuntimeError, match="at least"):
        settings.validate()


def test_negative_quantum_is_refused():
    settings = config.Settings(environment="development", distance_quantum_m=-1.0)

    with pytest.raises(RuntimeError, match="DISTANCE_QUANTUM_M"):
        settings.validate()


# --- headers --------------------------------------------------------------


def test_auth_responses_are_not_cacheable(client):
    response = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_admin_ui_pages_carry_a_strict_csp(client):
    response = client.get("/admin/ui/login")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "form-action 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"


def test_api_responses_do_not_carry_a_csp(client):
    response = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert "content-security-policy" not in response.headers
