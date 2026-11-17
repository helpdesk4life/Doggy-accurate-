import os
from dataclasses import dataclass

# The secret that ships in the repo. Anyone who reads this file can mint tokens
# for a deployment that forgot to override it, so it is only ever acceptable in
# development. See Settings.validate().
DEV_JWT_SECRET = "dev-insecure-secret-change-me-0123456789"

# HS256 wants at least 32 bytes of key material.
MIN_JWT_SECRET_BYTES = 32

DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "dev", "local", "test"})

# starttls upgrades a plain connection (port 587), ssl wraps it from the first
# byte (port 465), none is for a relay on the same host.
SMTP_SECURITY_MODES = frozenset({"starttls", "ssl", "none"})


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, all overridable through the environment."""

    # Which deployment this is. Anything outside DEVELOPMENT_ENVIRONMENTS is
    # treated as production and gets the strict startup checks.
    environment: str = os.getenv("DOGWALK_ENV", "development")

    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    # A separate Redis for the disposable geo index. Empty means "same instance
    # as REDIS_URL". Point it at an instance with persistence off: live GPS
    # positions are the most sensitive data in the deployment and must not be
    # written to the AOF of the persistent store. See docker-compose.yml.
    geo_redis_url: str = os.getenv("GEO_REDIS_URL", "")
    proximity_radius_m: float = float(os.getenv("PROXIMITY_RADIUS_M", "500"))
    walker_ttl_seconds: float = float(os.getenv("WALKER_TTL_SECONDS", "30"))
    sweep_interval_seconds: float = float(os.getenv("SWEEP_INTERVAL_SECONDS", "5"))

    # Privacy budget for PROXIMITY_ALERT. Neighbours are described as a distance
    # and a bearing, both snapped to these grids, so no absolute position ever
    # leaves the server. Tightening them makes the map more accurate and the
    # walker more identifiable; they move together.
    distance_quantum_m: float = float(os.getenv("DISTANCE_QUANTUM_M", "25"))
    bearing_quantum_deg: float = float(os.getenv("BEARING_QUANTUM_DEG", "30"))

    # Auth. The default secret is deliberately obvious so an unconfigured
    # deployment is easy to spot; docker-compose sets a real one. It is 40 bytes
    # because HS256 wants at least 32.
    jwt_secret: str = os.getenv("JWT_SECRET", DEV_JWT_SECRET)
    access_token_ttl_seconds: int = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900"))
    refresh_token_ttl_seconds: int = int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", "2592000"))

    # Admin sessions are deliberately shorter than a user's refresh token and
    # there is no refresh flow: an operator signs in again when this expires.
    # A stolen admin token is worth far more than a stolen walker token, and
    # re-entering a password once a day is a cheap price for that.
    admin_token_ttl_seconds: int = int(os.getenv("ADMIN_TOKEN_TTL_SECONDS", "28800"))

    # The ticket an account gets when it still has the password an operator
    # handed it. It opens one endpoint, so it only has to outlive the form:
    # fifteen minutes is longer than anyone takes to type a password twice.
    password_change_token_ttl_seconds: int = int(
        os.getenv("PASSWORD_CHANGE_TOKEN_TTL_SECONDS", "900")
    )

    # Used to build the link in a password-reset email.
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

    # Per-IP throttling on the auth endpoints. Off only in tests that need to
    # drive the per-email lockout without tripping the IP limit first.
    rate_limit_enabled: bool = os.getenv("RATE_LIMIT_ENABLED", "1") not in {"0", "false", "no"}

    # Registration needs a single-use code. This is what stops /auth/register
    # being used to ask whether an address has an account: the code is checked
    # before the email is looked up, so an uninvited probe gets the same answer
    # either way. See app/invites.py. Turn it off only for a public launch, and
    # only once there is email verification to replace it.
    invite_required: bool = os.getenv("INVITE_REQUIRED", "1") not in {"0", "false", "no"}

    # Which EmailSender to install. "logging" writes the whole message —
    # including a password-reset code — to the backend log, so validate() refuses
    # it outside development unless it is acknowledged. "smtp" is the real one.
    email_sender: str = os.getenv("EMAIL_SENDER", "logging")

    # SMTP, used when EMAIL_SENDER=smtp. Any provider that speaks the protocol
    # works, so nothing here is provider-specific.
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    # The envelope sender. Providers usually require it to be an address verified
    # with them, so it is not derived from SMTP_USERNAME.
    smtp_from: str = os.getenv("SMTP_FROM", "")
    smtp_security: str = os.getenv("SMTP_SECURITY", "starttls")
    smtp_timeout_seconds: float = float(os.getenv("SMTP_TIMEOUT_SECONDS", "15"))

    # The acknowledgement. A reset code in a log is a working account takeover for
    # anyone who can read that log, so the default is to refuse to boot rather
    # than to warn. Set this only on a box whose logs nobody else can read.
    allow_logging_email: bool = os.getenv("ALLOW_LOGGING_EMAIL", "0") not in {"0", "false", "no"}

    @property
    def is_development(self) -> bool:
        return self.environment.strip().lower() in DEVELOPMENT_ENVIRONMENTS

    @property
    def jwt_secret_is_default(self) -> bool:
        return self.jwt_secret == DEV_JWT_SECRET

    def validate(self) -> None:
        """Refuse to start in a configuration that would be trivially forgeable.

        A warning is not enough for the secret: a deployment that logs a warning
        and carries on is a deployment where anyone who has read this repository
        can mint an access token for any account. Failing to boot is the only
        behaviour that gets noticed.
        """
        if self.jwt_secret_is_default and not self.is_development:
            raise RuntimeError(
                "JWT_SECRET is unset and DOGWALK_ENV is "
                f"{self.environment!r}. The built-in development secret is public, "
                "so every token would be forgeable. Set JWT_SECRET to at least "
                f"{MIN_JWT_SECRET_BYTES} random bytes, or set DOGWALK_ENV=development "
                "if this really is a development machine."
            )

        if len(self.jwt_secret.encode()) < MIN_JWT_SECRET_BYTES:
            raise RuntimeError(
                f"JWT_SECRET is {len(self.jwt_secret.encode())} bytes; "
                f"HS256 needs at least {MIN_JWT_SECRET_BYTES}. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )

        if not self.is_development:
            # Password-reset links are built from this, so a wrong value sends
            # working reset links somewhere that is not this deployment - or over
            # plain http. Development keeps the localhost default.
            base = self.public_base_url.strip().lower()
            if not base.startswith("https://") or base.startswith(
                ("https://localhost", "https://127.0.0.1", "https://[::1]")
            ):
                raise RuntimeError(
                    f"PUBLIC_BASE_URL is {self.public_base_url!r} and DOGWALK_ENV is "
                    f"{self.environment!r}. Reset links are built from it, so outside "
                    "development it must be the https URL this deployment is actually "
                    "served on, e.g. https://backend.example.com."
                )

        if self.distance_quantum_m < 0 or self.bearing_quantum_deg < 0:
            raise RuntimeError(
                "DISTANCE_QUANTUM_M and BEARING_QUANTUM_DEG must not be negative. "
                "Zero disables quantisation, which broadcasts positions to within "
                "a few metres; only do that on a development machine."
            )

        if (
            self.email_sender == "logging"
            and not self.is_development
            and not self.allow_logging_email
        ):
            raise RuntimeError(
                "EMAIL_SENDER is 'logging', which writes password-reset codes to "
                f"the backend log, and DOGWALK_ENV is {self.environment!r}. Anyone "
                "who can read that log can reset any account, so this is refused "
                "rather than warned about. Implement a real sender in "
                "app/email.py and set EMAIL_SENDER to it, or set "
                "ALLOW_LOGGING_EMAIL=1 if the logs are readable only by you."
            )

        if self.email_sender == "smtp":
            missing = [
                name
                for name, value in (
                    ("SMTP_HOST", self.smtp_host),
                    ("SMTP_FROM", self.smtp_from),
                )
                if not value.strip()
            ]
            if missing:
                raise RuntimeError(
                    f"EMAIL_SENDER is 'smtp' but {' and '.join(missing)} "
                    f"{'is' if len(missing) == 1 else 'are'} empty. Without a host "
                    "there is nowhere to send, and without a sender address the "
                    "provider rejects the message. Set both, or use "
                    "EMAIL_SENDER=logging on a development machine."
                )

            if self.smtp_security not in SMTP_SECURITY_MODES:
                raise RuntimeError(
                    f"SMTP_SECURITY is {self.smtp_security!r}. Use 'starttls' "
                    "(port 587), 'ssl' (port 465) or 'none' (a relay on this host)."
                )

            if self.smtp_security == "none" and self.smtp_password:
                raise RuntimeError(
                    "SMTP_SECURITY is 'none' but SMTP_PASSWORD is set, so the "
                    "password would cross the network in the clear. Use 'starttls' "
                    "or 'ssl', or drop the password if the relay is on this host."
                )


settings = Settings()
