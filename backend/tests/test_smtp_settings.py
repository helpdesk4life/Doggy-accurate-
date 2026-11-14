"""The admin panel's SMTP configuration: the store, the API and the page.

The property worth protecting is that a saved configuration takes effect without
a restart, and that the password never comes back out — not in the JSON, not in
the HTML, not in the audit log.
"""

import asyncio
import json
import smtplib

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import admin, config, email, smtp_settings, users
from app.main import create_app

ADMIN_EMAIL = "operator@example.com"
ADMIN_PASSWORD = "operator-password"
PASSWORD = "correct-horse-battery"


class FakeSmtp:
    """Records what a real ``smtplib.SMTP`` would have been asked to do."""

    instances: list["FakeSmtp"] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.calls: list[tuple] = []
        FakeSmtp.instances.append(self)

    def starttls(self, context=None) -> None:
        self.calls.append(("starttls",))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username))

    def send_message(self, message) -> None:
        self.calls.append(("send_message", message))

    def quit(self) -> None:
        self.calls.append(("quit",))

    def close(self) -> None:
        self.calls.append(("close",))


@pytest.fixture
def smtp(monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    return FakeSmtp.instances


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


def bearer(client) -> dict:
    response = client.post(
        "/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def payload(**overrides) -> dict:
    body = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "apikey",
        "password": "s3cret",
        "sender": "no-reply@example.com",
        "security": "starttls",
    }
    body.update(overrides)
    return body


def stored(**overrides) -> smtp_settings.SmtpConfig:
    return smtp_settings.SmtpConfig(**payload(**overrides))


def keys(redis_client, pattern: str) -> list[str]:
    async def scan():
        return [key async for key in redis_client.scan_iter(match=pattern)]

    return asyncio.run(scan())


# --- the store ------------------------------------------------------------


def test_a_saved_configuration_round_trips(redis_client):
    asyncio.run(smtp_settings.save(redis_client, stored()))

    assert asyncio.run(smtp_settings.load(redis_client)) == stored()


def test_nothing_saved_reads_as_none(redis_client):
    assert asyncio.run(smtp_settings.load(redis_client)) is None


def test_clearing_forgets_it(redis_client):
    asyncio.run(smtp_settings.save(redis_client, stored()))
    asyncio.run(smtp_settings.clear(redis_client))

    assert asyncio.run(smtp_settings.load(redis_client)) is None


# --- which configuration wins ---------------------------------------------


def test_a_saved_configuration_beats_the_environment(redis_client):
    asyncio.run(smtp_settings.save(redis_client, stored(host="panel.example.com")))

    resolved = asyncio.run(smtp_settings.resolve(redis_client, config.Settings()))

    assert resolved.host == "panel.example.com"


def test_the_environment_is_used_when_email_sender_says_smtp(redis_client):
    environment = config.Settings(
        email_sender="smtp", smtp_host="env.example.com", smtp_from="no-reply@example.com"
    )

    resolved = asyncio.run(smtp_settings.resolve(redis_client, environment))

    assert resolved.host == "env.example.com"


def test_the_environment_is_ignored_when_email_sender_says_logging(redis_client):
    """A deployment that never touches the panel behaves as documented."""
    environment = config.Settings(
        email_sender="logging", smtp_host="env.example.com", smtp_from="no-reply@example.com"
    )

    assert asyncio.run(smtp_settings.resolve(redis_client, environment)) is None


def test_an_incomplete_saved_configuration_falls_through(redis_client):
    asyncio.run(smtp_settings.save(redis_client, stored(host="")))

    assert asyncio.run(smtp_settings.resolve(redis_client, config.Settings())) is None


@pytest.mark.parametrize(
    "broken, expected",
    [
        (stored(host=""), "host"),
        (stored(sender=""), "sender"),
        (stored(security="tls"), "security"),
        (stored(security="none"), "unencrypted"),
    ],
)
def test_a_broken_configuration_says_what_is_wrong(broken, expected):
    assert expected in broken.problem()


def test_a_complete_configuration_has_no_problem():
    assert stored().problem() is None


# --- the JSON API ---------------------------------------------------------


def test_the_api_needs_an_admin_token(client, operator):
    assert client.get("/admin/smtp").status_code == 401


def test_saving_switches_the_running_sender(client, operator, smtp):
    assert isinstance(client.app.state.email_sender, email.LoggingEmailSender)

    response = client.put("/admin/smtp", json=payload(), headers=bearer(client))

    assert response.status_code == 200, response.text
    assert isinstance(client.app.state.email_sender, email.SmtpEmailSender)


def test_clearing_switches_back_to_the_log(client, operator, smtp):
    headers = bearer(client)
    client.put("/admin/smtp", json=payload(), headers=headers)

    assert client.delete("/admin/smtp", headers=headers).status_code == 204

    assert isinstance(client.app.state.email_sender, email.LoggingEmailSender)


def test_the_api_never_returns_the_password(client, operator):
    headers = bearer(client)
    client.put("/admin/smtp", json=payload(), headers=headers)

    body = client.get("/admin/smtp", headers=headers).json()

    assert body["has_password"] is True
    assert body["source"] == "panel"
    assert body["active"] is True
    assert "s3cret" not in json.dumps(body)


def test_an_incomplete_configuration_is_refused(client, operator):
    response = client.put("/admin/smtp", json=payload(host=""), headers=bearer(client))

    assert response.status_code == 400
    assert "host" in response.json()["detail"].lower()


def test_a_blank_password_keeps_the_stored_one(client, operator, redis_client):
    headers = bearer(client)
    client.put("/admin/smtp", json=payload(), headers=headers)

    client.put("/admin/smtp", json=payload(password=""), headers=headers)

    assert asyncio.run(smtp_settings.load(redis_client)).password == "s3cret"


def test_the_test_endpoint_sends_through_the_submitted_values(client, operator, smtp):
    response = client.post(
        "/admin/smtp/test", json=payload(to="you@example.com"), headers=bearer(client)
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert smtp[0].host == "smtp.example.com"
    assert smtp[0].calls[0][0] == "starttls"


def test_the_test_endpoint_does_not_save(client, operator, redis_client, smtp):
    client.post("/admin/smtp/test", json=payload(to="you@example.com"), headers=bearer(client))

    assert asyncio.run(smtp_settings.load(redis_client)) is None


def test_a_failed_test_reports_the_error(client, operator, smtp, monkeypatch):
    def explode(self, message):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(FakeSmtp, "send_message", explode)

    response = client.post(
        "/admin/smtp/test", json=payload(to="you@example.com"), headers=bearer(client)
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "SMTPAuthenticationError" in response.json()["detail"]


# --- the page -------------------------------------------------------------


def test_the_page_needs_the_cookie(client, operator):
    response = client.get("/admin/ui/smtp", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/login"


def test_the_page_says_nothing_is_configured(client, operator):
    sign_in(client)

    assert "Nothing configured" in client.get("/admin/ui/smtp").text


def test_saving_from_the_page_redirects_and_takes_effect(client, operator, smtp):
    sign_in(client)

    response = client.post("/admin/ui/smtp", data=payload(), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/smtp?saved=1"
    assert isinstance(client.app.state.email_sender, email.SmtpEmailSender)


def test_the_page_never_renders_the_password(client, operator):
    sign_in(client)
    client.post("/admin/ui/smtp", data=payload())

    body = client.get("/admin/ui/smtp").text

    assert "s3cret" not in body
    assert "A password is stored" in body


def test_the_test_button_sends_and_reports(client, operator, smtp):
    sign_in(client)

    response = client.post(
        "/admin/ui/smtp", data={**payload(), "action": "test", "test_to": "you@example.com"}
    )

    assert response.status_code == 200
    assert "Test message sent to you@example.com" in response.text
    assert smtp[0].host == "smtp.example.com"


def test_the_test_button_needs_a_recipient(client, operator, smtp):
    sign_in(client)

    response = client.post(
        "/admin/ui/smtp", data={**payload(), "action": "test", "test_to": ""}
    )

    assert response.status_code == 400
    assert "Enter an address" in response.text
    assert smtp == []


def test_a_bad_port_is_a_message_not_a_crash(client, operator):
    sign_in(client)

    response = client.post("/admin/ui/smtp", data=payload(port="not-a-port"))

    assert response.status_code == 400
    assert "port must be a number" in response.text
    # The form keeps what was typed rather than wiping it.
    assert 'value="smtp.example.com"' in response.text


def test_forgetting_from_the_page_clears_it(client, operator, redis_client, smtp):
    sign_in(client)
    client.post("/admin/ui/smtp", data=payload())

    response = client.post("/admin/ui/smtp/clear", follow_redirects=False)

    assert response.status_code == 303
    assert asyncio.run(smtp_settings.load(redis_client)) is None
    assert isinstance(client.app.state.email_sender, email.LoggingEmailSender)


# --- the reset code that outlived its account -----------------------------


def test_deleting_an_account_takes_its_pending_reset_code(client, redis_client):
    client.post("/auth/register", json={"email": "alice@example.com", "password": PASSWORD})
    client.post("/auth/password-reset/request", json={"email": "alice@example.com"})

    assert len(keys(redis_client, "dogwalk:reset:*")) == 2  # the code and its index

    account = asyncio.run(users.get_by_email(redis_client, "alice@example.com"))
    asyncio.run(users.delete_user(redis_client, account["user_id"]))

    assert keys(redis_client, "dogwalk:reset:*") == []


def test_consuming_a_code_clears_its_index_entry(client, redis_client):
    client.post("/auth/register", json={"email": "alice@example.com", "password": PASSWORD})

    code = asyncio.run(users.create_reset_code(redis_client, "alice@example.com"))
    assert len(keys(redis_client, "dogwalk:reset:index:*")) == 1

    assert asyncio.run(users.consume_reset_code(redis_client, "alice@example.com", code))
    assert keys(redis_client, "dogwalk:reset:index:*") == []
