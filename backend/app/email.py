"""Outbound email.

There is exactly one implementation today: a sender that writes the message to
the log. That is deliberate. The password-reset flow is complete and testable
without an email provider.

To go live, implement ``EmailSender`` against your provider, add it to
``build_sender``, and set ``EMAIL_SENDER`` to its name. ``Settings.validate()``
refuses to boot outside development while the configured sender is the logging
one, because a reset code in a log is a working account takeover for anyone who
can read that log.
"""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class LoggingEmailSender:
    """Development sender: writes the message to the log instead of sending it."""

    async def send(self, to: str, subject: str, body: str) -> None:
        logger.info("email to %s | %s\n%s", to, subject, body)


def build_sender(name: str) -> EmailSender:
    """Construct the sender named by ``EMAIL_SENDER``.

    Add providers here. The name is resolved at startup rather than at the first
    reset request, so a typo fails the boot instead of silently dropping mail.
    """
    if name == "logging":
        return LoggingEmailSender()
    raise RuntimeError(f"Unknown EMAIL_SENDER {name!r}. Known senders: logging.")


def reset_email_body(code: str, ttl_minutes: int) -> str:
    return (
        "Someone asked to reset the password for your DogWalk account.\n\n"
        f"Your code is: {code}\n\n"
        f"It expires in {ttl_minutes} minutes and can only be used once.\n"
        "If this was not you, ignore this message; your password has not changed.\n"
    )
