"""Where the SMTP configuration lives.

Two sources, one rule: a complete configuration saved by an operator in the admin
panel wins over the environment. That is what makes the panel worth having —
saving takes effect on the next send, with no restart and no `.env` edit — while
an environment-only deployment keeps working exactly as before.

The password is stored as it is typed. It has to be: SMTP authentication needs
the plaintext, unlike a user password which is only ever compared against a hash.
It is never returned to a browser, and it is no more exposed here than in the
`.env` file that holds it on the other configuration path.
"""

import dataclasses

KEY = "dogwalk:settings:smtp"

# starttls upgrades a plain connection (port 587), ssl wraps it from the first
# byte (port 465), none is for a relay on the same host.
SECURITY_MODES = ("starttls", "ssl", "none")


@dataclasses.dataclass(frozen=True)
class SmtpConfig:
    host: str = ""
    port: int = 587
    sender: str = ""
    username: str = ""
    password: str = ""
    security: str = "starttls"
    timeout: float = 15.0

    def problem(self) -> str | None:
        """Why this configuration cannot be used, in words an operator can act on."""
        if not self.host.strip():
            return "The server host is empty."
        if not self.sender.strip():
            return "The sender address is empty. Providers reject a message without one."
        if self.security not in SECURITY_MODES:
            return f"{self.security!r} is not a security mode. Use starttls, ssl or none."
        if self.security == "none" and self.password:
            return (
                "A password is set but the connection is unencrypted, so it would "
                "cross the network in the clear. Use starttls or ssl."
            )
        return None


def from_settings(settings) -> SmtpConfig:
    """The configuration the environment describes."""
    return SmtpConfig(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.smtp_from,
        username=settings.smtp_username,
        password=settings.smtp_password,
        security=settings.smtp_security,
        timeout=settings.smtp_timeout_seconds,
    )


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def load(redis) -> SmtpConfig | None:
    """The saved configuration, or None if an operator has never saved one."""
    raw = await redis.hgetall(KEY)
    if not raw:
        return None

    fields = {_decode(key): _decode(value) for key, value in raw.items()}
    try:
        port = int(fields.get("port", 587))
    except ValueError:
        port = 587

    return SmtpConfig(
        host=fields.get("host", ""),
        port=port,
        sender=fields.get("sender", ""),
        username=fields.get("username", ""),
        password=fields.get("password", ""),
        security=fields.get("security", "starttls"),
    )


async def save(redis, config: SmtpConfig) -> None:
    await redis.hset(
        KEY,
        mapping={
            "host": config.host.strip(),
            "port": str(config.port),
            "sender": config.sender.strip(),
            "username": config.username.strip(),
            "password": config.password,
            "security": config.security,
        },
    )


async def clear(redis) -> None:
    await redis.delete(KEY)


async def resolve(redis, settings) -> SmtpConfig | None:
    """The configuration to send with, or None to fall back to the log.

    A saved configuration is preferred whenever it is usable, even if
    ``EMAIL_SENDER`` still says ``logging``: an operator who filled in the panel
    meant it. The environment is only consulted when ``EMAIL_SENDER=smtp``, so a
    deployment that never touches the panel behaves exactly as documented.
    """
    stored = await load(redis)
    if stored is not None and stored.problem() is None:
        return stored

    if settings.email_sender == "smtp":
        environment = from_settings(settings)
        if environment.problem() is None:
            return environment

    return None
