import os
from dataclasses import dataclass

# The secret that ships in the repo. Anyone who reads this file can mint tokens
# for a deployment that forgot to override it, so it is only ever acceptable in
# development. See Settings.validate().
DEV_JWT_SECRET = "dev-insecure-secret-change-me-0123456789"

# HS256 wants at least 32 bytes of key material.
MIN_JWT_SECRET_BYTES = 32

DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "dev", "local", "test"})


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, all overridable through the environment."""

    # Which deployment this is. Anything outside DEVELOPMENT_ENVIRONMENTS is
    # treated as production and gets the strict startup checks.
    environment: str = os.getenv("DOGWALK_ENV", "development")

    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
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

    # Which EmailSender to install. Only "logging" exists today, and it writes the
    # whole message — including a password-reset code — to the backend log. See
    # validate(): outside development that is refused unless it is acknowledged.
    email_sender: str = os.getenv("EMAIL_SENDER", "logging")

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


settings = Settings()
