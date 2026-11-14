"""Outbound email.

Two implementations: one that writes the message to the log, and one that speaks
SMTP. The logging sender exists so the password-reset flow is complete and
testable without a provider; it is refused outside development, because a reset
code in a log is a working account takeover for anyone who can read that log.
See ``Settings.validate()``.

To go live, set ``EMAIL_SENDER=smtp`` and fill in the ``SMTP_*`` variables. Any
provider that speaks SMTP works — Resend, Postmark, Mailgun, Fastmail, a local
relay — so there is no provider-specific code to write.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from app import config

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class LoggingEmailSender:
    """Development sender: writes the message to the log instead of sending it."""

    async def send(self, to: str, subject: str, body: str) -> None:
        logger.info("email to %s | %s\n%s", to, subject, body)


class SmtpEmailSender:
    """Sends through any SMTP server.

    ``smtplib`` is blocking, so the whole conversation runs on a worker thread.
    A provider that takes ten seconds to answer must not stall the event loop
    for every other request in the process.
    """

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        security: str = "starttls",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = password
        self.security = security
        self.timeout = timeout

    async def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        await asyncio.to_thread(self._send_blocking, message)

    def _send_blocking(self, message: EmailMessage) -> None:
        if self.security == "ssl":
            client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            if self.security == "starttls":
                client.starttls(context=ssl.create_default_context())
            if self.username:
                client.login(self.username, self.password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                # The server may already have dropped the connection; closing
                # the socket is all that is left to do.
                client.close()


def build_sender(name: str, settings: config.Settings | None = None) -> EmailSender:
    """Construct the sender named by ``EMAIL_SENDER``.

    The name is resolved at startup rather than at the first reset request, so a
    typo fails the boot instead of silently dropping mail.
    """
    if name == "logging":
        return LoggingEmailSender()
    if name == "smtp":
        settings = settings or config.settings
        return SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            sender=settings.smtp_from,
            username=settings.smtp_username,
            password=settings.smtp_password,
            security=settings.smtp_security,
            timeout=settings.smtp_timeout_seconds,
        )
    raise RuntimeError(f"Unknown EMAIL_SENDER {name!r}. Known senders: logging, smtp.")


def reset_email_body(code: str, ttl_minutes: int) -> str:
    return (
        "Someone asked to reset the password for your DogWalk account.\n\n"
        f"Your code is: {code}\n\n"
        f"It expires in {ttl_minutes} minutes and can only be used once.\n"
        "If this was not you, ignore this message; your password has not changed.\n"
    )
