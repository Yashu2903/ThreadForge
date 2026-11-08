"""Application settings.

Every deploy-time decision is an explicit environment setting. Validation fails at startup with a
message that names the setting and says what to do, and never echoes secret values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[2]

CALLBACK_PATH = "/auth/callback"
GOOGLE_ISSUER = "https://accounts.google.com"

# Any value containing one of these is treated as an unfilled placeholder.
_PLACEHOLDER_MARKERS = ("replace-me", "replaceme", "change-me", "changeme", "placeholder", "your-")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MIN_PRODUCTION_SECRET_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised when settings are invalid. The message never contains secret values."""


def is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def origin_of(url: str) -> str:
    """scheme://host[:port], lowercased. Used for Origin comparisons."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: SecretStr
    readiness_timeout_seconds: float = 2.0

    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_issuer: str = GOOGLE_ISSUER
    google_discovery_url: str = f"{GOOGLE_ISSUER}/.well-known/openid-configuration"

    public_base_url: str = "http://localhost:8000"
    oidc_callback_url: str = ""
    frontend_success_url: str = ""
    frontend_error_url: str = ""
    trusted_origins: Annotated[list[str], NoDecode] = []

    # Part 2: invitation links point at the frontend's join page (the token is in the URL
    # fragment, which browsers never send to servers). Dev default: the API's own origin.
    frontend_base_url: str = ""
    invitation_default_hours: int = 24
    invitation_min_hours: int = 1
    invitation_max_hours: int = 168
    # Caps, enforced under row locks so they hold under concurrency.
    max_owned_rooms_per_user: int = 20
    max_members_per_room: int = 50
    max_active_invitations_per_room: int = 50

    session_lifetime_seconds: int = 7 * 24 * 3600
    login_attempt_lifetime_seconds: int = 600
    max_pending_login_attempts: int = 1000

    csrf_secret: SecretStr

    # Part 3. Keyed fingerprint of a message's creation request (see services/messages.py).
    # Rotating it makes in-flight retries fail with message_id_reused; retries happen within
    # seconds, so that is acceptable.
    message_fingerprint_secret: SecretStr
    message_writes_per_minute: int = 30

    trusted_proxy_ips: Annotated[list[str], NoDecode] = []
    cookie_secure: bool | None = None

    @field_validator("trusted_origins", "trusted_proxy_ips", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    # ---- derived values -------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def secure_cookies(self) -> bool:
        return bool(self.cookie_secure)

    @property
    def session_cookie_name(self) -> str:
        return "__Host-tf_session" if self.secure_cookies else "tf_session"

    @property
    def binding_cookie_name(self) -> str:
        return "__Host-tf_login_binding" if self.secure_cookies else "tf_login_binding"

    @property
    def oidc_configured(self) -> bool:
        return not is_placeholder(self.google_client_id) and not is_placeholder(
            self.google_client_secret.get_secret_value()
        )

    @property
    def database_name(self) -> str:
        return make_url(self.database_url.get_secret_value()).database or ""

    # ---- validation -----------------------------------------------------------------------

    @model_validator(mode="after")
    def _validate_and_fill_defaults(self) -> Settings:
        problems: list[str] = []
        prod = self.is_production

        # Cookie security: production is always Secure; dev may opt in explicitly.
        if prod:
            if self.cookie_secure is False:
                problems.append("COOKIE_SECURE=false is not allowed when APP_ENV=production.")
            self.cookie_secure = True
        elif self.cookie_secure is None:
            self.cookie_secure = False

        try:
            url = make_url(self.database_url.get_secret_value())
            if url.drivername != "postgresql+asyncpg" or not url.database:
                raise ValueError
        except Exception:
            problems.append(
                "DATABASE_URL must look like postgresql+asyncpg://user:password@host:port/dbname."
            )

        problems += self._check_url("PUBLIC_BASE_URL", self.public_base_url)
        base_origin = origin_of(self.public_base_url)

        # Development conveniences are filled in only outside production.
        if not prod:
            self.oidc_callback_url = self.oidc_callback_url or f"{base_origin}{CALLBACK_PATH}"
            self.frontend_success_url = self.frontend_success_url or f"{base_origin}/auth/me"
            self.frontend_error_url = self.frontend_error_url or f"{base_origin}/auth/me"
            self.frontend_base_url = self.frontend_base_url or base_origin
            self.trusted_origins = self.trusted_origins or [base_origin]
        else:
            for name, value in (
                ("OIDC_CALLBACK_URL", self.oidc_callback_url),
                ("FRONTEND_SUCCESS_URL", self.frontend_success_url),
                ("FRONTEND_ERROR_URL", self.frontend_error_url),
            ):
                if not value:
                    problems.append(f"{name} must be set explicitly in production.")
            if not self.trusted_origins:
                problems.append("TRUSTED_ORIGINS must list at least one origin in production.")

        for name, value in (
            ("OIDC_CALLBACK_URL", self.oidc_callback_url),
            ("FRONTEND_SUCCESS_URL", self.frontend_success_url),
            ("FRONTEND_ERROR_URL", self.frontend_error_url),
        ):
            if value:
                problems += self._check_url(name, value)

        if self.oidc_callback_url:
            callback = urlsplit(self.oidc_callback_url)
            if callback.path != CALLBACK_PATH or callback.query or callback.fragment:
                problems.append(f"OIDC_CALLBACK_URL must end with {CALLBACK_PATH} (no query).")
            if origin_of(self.oidc_callback_url) != base_origin:
                problems.append("OIDC_CALLBACK_URL must be on the same origin as PUBLIC_BASE_URL.")

        for origin in self.trusted_origins:
            parts = urlsplit(origin)
            if "*" in origin or parts.path not in ("", "/") or not parts.netloc:
                problems.append(
                    f"TRUSTED_ORIGINS entry {origin!r} must be an exact origin "
                    "like https://app.example.com (no wildcard, no path)."
                )
            else:
                problems += self._check_url("TRUSTED_ORIGINS", origin)
        self.trusted_origins = [origin_of(o) for o in self.trusted_origins if "*" not in o]

        # The identity provider endpoints are always https, in every environment.
        for name, value in (
            ("GOOGLE_ISSUER", self.google_issuer),
            ("GOOGLE_DISCOVERY_URL", self.google_discovery_url),
        ):
            if urlsplit(value).scheme != "https":
                problems.append(f"{name} must be an https URL.")

        csrf = self.csrf_secret.get_secret_value()
        if not csrf:
            problems.append(
                "CSRF_SECRET must be set (generate one with secrets.token_urlsafe(48))."
            )
        if prod:
            if len(csrf) < MIN_PRODUCTION_SECRET_LENGTH or is_placeholder(csrf):
                problems.append(
                    f"CSRF_SECRET must be a real random value of at least "
                    f"{MIN_PRODUCTION_SECRET_LENGTH} characters in production."
                )
            if not self.oidc_configured:
                problems.append(
                    "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in production."
                )

        fingerprint = self.message_fingerprint_secret.get_secret_value()
        if not fingerprint:
            problems.append(
                "MESSAGE_FINGERPRINT_SECRET must be set "
                "(generate one with secrets.token_urlsafe(48))."
            )
        elif prod and (
            len(fingerprint) < MIN_PRODUCTION_SECRET_LENGTH or is_placeholder(fingerprint)
        ):
            problems.append(
                f"MESSAGE_FINGERPRINT_SECRET must be a real random value of at least "
                f"{MIN_PRODUCTION_SECRET_LENGTH} characters in production."
            )
        if self.message_writes_per_minute < 1:
            problems.append("MESSAGE_WRITES_PER_MINUTE must be at least 1.")

        if prod and not self.frontend_base_url:
            problems.append("FRONTEND_BASE_URL must be set explicitly in production.")
        if self.frontend_base_url:
            problems += self._check_url("FRONTEND_BASE_URL", self.frontend_base_url)
            base = urlsplit(self.frontend_base_url)
            if base.query or base.fragment:
                problems.append("FRONTEND_BASE_URL must not contain a query or fragment.")
            self.frontend_base_url = self.frontend_base_url.rstrip("/")
        if (
            not 1
            <= self.invitation_min_hours
            <= self.invitation_default_hours
            <= (self.invitation_max_hours)
        ):
            problems.append(
                "INVITATION_MIN_HOURS <= INVITATION_DEFAULT_HOURS <= INVITATION_MAX_HOURS "
                "must hold, and the minimum must be at least 1."
            )
        for name, cap in (
            ("MAX_OWNED_ROOMS_PER_USER", self.max_owned_rooms_per_user),
            ("MAX_MEMBERS_PER_ROOM", self.max_members_per_room),
            ("MAX_ACTIVE_INVITATIONS_PER_ROOM", self.max_active_invitations_per_room),
        ):
            if cap < 1:
                problems.append(f"{name} must be at least 1.")

        if self.session_lifetime_seconds < 60:
            problems.append("SESSION_LIFETIME_SECONDS must be at least 60.")
        if not 30 <= self.login_attempt_lifetime_seconds <= 3600:
            problems.append("LOGIN_ATTEMPT_LIFETIME_SECONDS must be between 30 and 3600.")
        if self.max_pending_login_attempts < 1:
            problems.append("MAX_PENDING_LOGIN_ATTEMPTS must be at least 1.")

        if problems:
            raise ValueError("Invalid configuration:\n- " + "\n- ".join(problems))
        return self

    def _check_url(self, name: str, value: str) -> list[str]:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return [f"{name} must be an absolute http(s) URL."]
        if self.is_production and parts.scheme != "https":
            return [f"{name} must use https in production."]
        if parts.scheme == "http" and (parts.hostname or "") not in _LOCAL_HOSTS:
            return [f"{name}: plain http is only allowed for localhost development."]
        return []


def load_settings() -> Settings:
    """Build Settings, converting validation errors into a secret-free ConfigError."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        lines = []
        for err in exc.errors(include_input=False, include_url=False):
            where = ".".join(str(p) for p in err["loc"]).upper()
            message = str(err["msg"]).removeprefix("Value error, ")
            lines.append(f"{where}: {message}" if where else message)
        raise ConfigError("\n".join(lines)) from None


class _DatabaseOnlySettings(BaseSettings):
    """Just DATABASE_URL, so migrations and maintenance jobs do not need CSRF/Google settings."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )
    database_url: SecretStr


def load_database_url() -> str:
    try:
        return _DatabaseOnlySettings().database_url.get_secret_value()  # type: ignore[call-arg]
    except ValidationError:
        raise ConfigError("DATABASE_URL: must be set (see .env.example).") from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
