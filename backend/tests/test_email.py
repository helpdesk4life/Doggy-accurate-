"""The SMTP sender and the configuration that guards it."""

import asyncio
import smtplib

import pytest

from app import config, email


class FakeSmtp:
    """Records what a real ``smtplib.SMTP`` would have been asked to do."""

    instances: list["FakeSmtp"] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple] = []
        FakeSmtp.instances.append(self)

    def starttls(self, context=None) -> None:
        self.calls.append(("starttls", context is not None))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def send_message(self, message) -> None:
        self.calls.append(("send_message", message))

    def quit(self) -> None:
        self.calls.append(("quit",))

    def close(self) -> None:
        self.calls.append(("close",))


@pytest.fixture
def smtp(monkeypatch):
    """Replace both SMTP classes and hand back the connections that were made."""
    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    return FakeSmtp.instances


def sender(**overrides) -> email.SmtpEmailSender:
    options = {
        "host": "smtp.example.com",
        "port": 587,
        "sender": "Mikywalk <no-reply@example.com>",
        "username": "apikey",
        "password": "s3cret",
    }
    options.update(overrides)
    return email.SmtpEmailSender(**options)


def send(instance: email.SmtpEmailSender, to: str = "alice@example.com") -> None:
    asyncio.run(instance.send(to=to, subject="Your code", body="Your code is: 123456"))


# --- the conversation with the server -------------------------------------


def test_starttls_is_the_default_and_runs_before_login(smtp):
    send(sender())

    connection = smtp[0]
    assert connection.host == "smtp.example.com"
    assert connection.port == 587
    assert [call[0] for call in connection.calls] == [
        "starttls",
        "login",
        "send_message",
        "quit",
    ]
    # A real TLS context, not the unverified default.
    assert connection.calls[0][1] is True


def test_ssl_connects_over_tls_and_skips_starttls(smtp):
    send(sender(port=465, security="ssl"))

    assert [call[0] for call in smtp[0].calls] == ["login", "send_message", "quit"]


def test_none_skips_tls_and_login_when_there_are_no_credentials(smtp):
    send(sender(port=25, security="none", username="", password=""))

    assert [call[0] for call in smtp[0].calls] == ["send_message", "quit"]


def test_the_message_carries_the_headers_a_provider_needs(smtp):
    send(sender())

    message = smtp[0].calls[2][1]
    assert message["From"] == "Mikywalk <no-reply@example.com>"
    assert message["To"] == "alice@example.com"
    assert message["Subject"] == "Your code"
    assert "123456" in message.get_content()


def test_a_failed_send_still_closes_the_connection(smtp, monkeypatch):
    def explode(self, message):
        raise smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr(FakeSmtp, "send_message", explode)

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        send(sender())

    assert ("quit",) in smtp[0].calls


def test_a_dead_connection_is_closed_when_quit_fails(smtp, monkeypatch):
    def explode(self):
        raise smtplib.SMTPServerDisconnected("gone")

    monkeypatch.setattr(FakeSmtp, "quit", explode)

    send(sender())

    assert ("close",) in smtp[0].calls


# --- startup checks -------------------------------------------------------


def smtp_settings(**overrides) -> config.Settings:
    options = {
        "environment": "production",
        "jwt_secret": "a" * 48,
        "public_base_url": "https://backend.example.com",
        "email_sender": "smtp",
        "smtp_host": "smtp.example.com",
        "smtp_from": "no-reply@example.com",
    }
    options.update(overrides)
    return config.Settings(**options)


def test_a_complete_smtp_configuration_boots():
    smtp_settings().validate()


def test_smtp_without_a_host_is_refused():
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        smtp_settings(smtp_host="").validate()


def test_smtp_without_a_sender_address_is_refused():
    with pytest.raises(RuntimeError, match="SMTP_FROM"):
        smtp_settings(smtp_from="").validate()


def test_an_unknown_smtp_security_mode_is_refused():
    with pytest.raises(RuntimeError, match="SMTP_SECURITY"):
        smtp_settings(smtp_security="tls").validate()


def test_a_password_over_an_unencrypted_connection_is_refused():
    with pytest.raises(RuntimeError, match="in the clear"):
        smtp_settings(smtp_security="none", smtp_password="s3cret").validate()


def test_the_smtp_sender_is_built_from_the_settings():
    built = email.build_sender(
        "smtp", smtp_settings(smtp_username="apikey", smtp_password="s3cret")
    )

    assert isinstance(built, email.SmtpEmailSender)
    assert built.host == "smtp.example.com"
    assert built.sender == "no-reply@example.com"
    assert built.security == "starttls"
