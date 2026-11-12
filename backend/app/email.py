"""Outbound email.

There is exactly one implementation today: a sender that writes the message to
the log. That is deliberate. The password-reset flow is complete and testable
without an email provider, and swapping in a real one is a single class plus one
line in ``main.py``.

To go live, implement ``EmailSender`` against your provider and assign it to
``app.state.email_sender`` in the lifespan. Nothing else changes.
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


def reset_email_body(code: str, ttl_minutes: int) -> str:
    return (
        "Someone asked to reset the password for your DogWalk account.\n\n"
        f"Your code is: {code}\n\n"
        f"It expires in {ttl_minutes} minutes and can only be used once.\n"
        "If this was not you, ignore this message; your password has not changed.\n"
    )
