"""Application settings.

Every deploy-time decision is an explicit environment setting. Validation fails at startup with a
message that names the setting and says what to do, and never echoes secret values.
"""

from __future__ import annotations

import re
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
DEFAULT_DEV_REDIS_URL = "redis://127.0.0.1:6390/0"
_CHANNEL_PREFIX = re.compile(r"[A-Za-z0-9._-]{1,64}")

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

    # Part 4: Redis/Valkey Pub/Sub. A live-notification hint, never storage. Production requires
    # TLS (rediss://) and a password. Never exposed to browsers; the app works without it.
    redis_url: SecretStr = SecretStr("")
    redis_channel_prefix: str = "threadforge"
    redis_socket_timeout_seconds: float = 5.0
    redis_health_check_interval_seconds: int = 30

    # Part 4: the outbox dispatcher (a separate process).
    outbox_batch_size: int = 50
    outbox_poll_interval_ms: int = 200
    outbox_claim_lease_seconds: float = 30.0
    outbox_publish_timeout_seconds: float = 2.0
    outbox_retry_max_delay_seconds: float = 60.0
    outbox_processed_retention_days: int = 7

    # Part 4/5: WebSockets (per API process).
    ws_max_connections_per_user: int = 5
    ws_max_connections_per_process: int = 1000
    ws_send_timeout_seconds: float = 5.0  # every socket send is bounded by this
    ws_connection_queue_size: int = 256  # per-connection outgoing frame queue
    ws_subscribe_timeout_seconds: float = 5.0  # wait for Redis's SUBSCRIBE acknowledgement

    # Part 5: resume / replay / flow control.
    ws_resume_timeout_seconds: float = 5.0  # first client frame must be `resume` within this
    replay_page_size: int = 100  # events per database read while draining
    replay_max_events: int = 1000  # a client further behind than this must resync
    replay_concurrency: int = 20  # process-wide cap on connections replaying at once
    replay_slot_wait_seconds: float = 10.0  # waiting longer than this for a slot => close 1013
    ack_window: int = 200  # max events sent but not yet acknowledged
    ack_timeout_seconds: float = 30.0  # a paused connection with no ack progress => close 4429
    ws_client_frames_per_second: float = 20.0  # sustained client frame budget (burst = 2 s)
    ws_ack_frames_per_second: float = 10.0
    ws_typing_frames_per_second: float = 2.0  # extras are dropped, not fatal

    # Part 5: lost-notification detection and live access enforcement.
    reconcile_interval_seconds: float = 5.0
    reconcile_interval_degraded_seconds: float = 2.0
    access_check_interval_seconds: float = 15.0
    access_cache_seconds: float = 1.0
    access_db_grace_seconds: float = 30.0

    # Part 5: heartbeat, presence and typing.
    client_heartbeat_interval_seconds: float = 15.0  # what clients are told to send
    ws_watchdog_timeout_seconds: float = 45.0  # no client frame of any kind => close 4408
    presence_expiry_seconds: float = 45.0
    presence_tick_seconds: float = 15.0
    presence_hint_min_interval_seconds: float = 1.0  # coalescing of hint-triggered ticks
    typing_expiry_seconds: float = 5.0
    typing_state_min_interval_seconds: float = 0.5  # at most 2 typing_state frames/s per room

    # Part 6: Google Drive import (a SEPARATE authorization from login). If any of these is missing
    # the feature is simply unavailable and GET /capabilities says not_configured.
    google_drive_client_id: str = ""
    google_drive_client_secret: SecretStr = SecretStr("")
    google_picker_api_key: str = ""
    google_cloud_project_number: str = ""
    # key_id:base64key pairs, comma separated; the FIRST is the active encryption key.
    cloud_token_encryption_keys: SecretStr = SecretStr("")

    # Part 6: documents. The limits mirror docs/document-schema.md; exceeding one is an error,
    # never a truncation.
    document_max_serialized_bytes: int = 1024 * 1024
    document_max_text_codepoints: int = 100_000
    document_max_total_nodes: int = 60_000
    document_max_blocks: int = 5_000  # block nodes plus list items
    document_max_inline_nodes: int = 20_000
    document_max_images: int = 100
    document_max_list_depth: int = 3
    document_max_link_length: int = 2048
    document_font_keys: Annotated[list[str], NoDecode] = [
        "sans",
        "serif",
        "mono",
        "calibri",
        "cambria",
        "arial",
        "times_new_roman",
        "courier_new",
    ]
    max_documents_per_room: int = 200
    document_writes_per_minute: int = 120  # per user per room; separate from chat's quota
    document_save_body_limit_bytes: int = 2 * 1024 * 1024  # PUT .../content
    # Also bounds a save's JSON overhead; the default document limit is 1 MiB of canonical text.

    trusted_proxy_ips: Annotated[list[str], NoDecode] = []
    cookie_secure: bool | None = None

    @field_validator("trusted_origins", "trusted_proxy_ips", "document_font_keys", mode="before")
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
    def drive_configured(self) -> bool:
        """True only when EVERY Drive setting is present (and not an unfilled placeholder)."""
        return not any(
            is_placeholder(value)
            for value in (
                self.google_drive_client_id,
                self.google_drive_client_secret.get_secret_value(),
                self.google_picker_api_key,
                self.google_cloud_project_number,
                self.cloud_token_encryption_keys.get_secret_value(),
            )
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
            if not self.redis_url.get_secret_value():
                self.redis_url = SecretStr(DEFAULT_DEV_REDIS_URL)
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
        problems += self._validate_part4(prod)
        problems += self._validate_part6()
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

    def _validate_part4(self, prod: bool) -> list[str]:
        """Redis, outbox and WebSocket settings. Messages never include the Redis password."""
        problems: list[str] = []
        redis_url = urlsplit(self.redis_url.get_secret_value())
        if redis_url.scheme not in ("redis", "rediss") or not redis_url.hostname:
            problems.append("REDIS_URL must be a redis:// or rediss:// URL.")
        elif prod:
            if redis_url.scheme != "rediss":
                problems.append("REDIS_URL must use TLS (rediss://) in production.")
            if not redis_url.password:
                problems.append("REDIS_URL must include a password in production.")
        elif redis_url.scheme == "redis" and redis_url.hostname not in _LOCAL_HOSTS:
            problems.append("REDIS_URL: plain redis:// is only allowed for localhost development.")
        if not _CHANNEL_PREFIX.fullmatch(self.redis_channel_prefix):
            problems.append("REDIS_CHANNEL_PREFIX must be 1-64 characters of A-Z a-z 0-9 . _ -")
        positives = {
            "REDIS_SOCKET_TIMEOUT_SECONDS": self.redis_socket_timeout_seconds,
            "OUTBOX_BATCH_SIZE": self.outbox_batch_size,
            "OUTBOX_POLL_INTERVAL_MS": self.outbox_poll_interval_ms,
            "OUTBOX_CLAIM_LEASE_SECONDS": self.outbox_claim_lease_seconds,
            "OUTBOX_PUBLISH_TIMEOUT_SECONDS": self.outbox_publish_timeout_seconds,
            "OUTBOX_RETRY_MAX_DELAY_SECONDS": self.outbox_retry_max_delay_seconds,
            "WS_MAX_CONNECTIONS_PER_USER": self.ws_max_connections_per_user,
            "WS_MAX_CONNECTIONS_PER_PROCESS": self.ws_max_connections_per_process,
            "WS_SEND_TIMEOUT_SECONDS": self.ws_send_timeout_seconds,
            "WS_CONNECTION_QUEUE_SIZE": self.ws_connection_queue_size,
            "WS_SUBSCRIBE_TIMEOUT_SECONDS": self.ws_subscribe_timeout_seconds,
            "WS_RESUME_TIMEOUT_SECONDS": self.ws_resume_timeout_seconds,
            "REPLAY_PAGE_SIZE": self.replay_page_size,
            "REPLAY_MAX_EVENTS": self.replay_max_events,
            "REPLAY_CONCURRENCY": self.replay_concurrency,
            "REPLAY_SLOT_WAIT_SECONDS": self.replay_slot_wait_seconds,
            "ACK_WINDOW": self.ack_window,
            "ACK_TIMEOUT_SECONDS": self.ack_timeout_seconds,
            "WS_CLIENT_FRAMES_PER_SECOND": self.ws_client_frames_per_second,
            "WS_ACK_FRAMES_PER_SECOND": self.ws_ack_frames_per_second,
            "WS_TYPING_FRAMES_PER_SECOND": self.ws_typing_frames_per_second,
            "RECONCILE_INTERVAL_SECONDS": self.reconcile_interval_seconds,
            "RECONCILE_INTERVAL_DEGRADED_SECONDS": self.reconcile_interval_degraded_seconds,
            "ACCESS_CHECK_INTERVAL_SECONDS": self.access_check_interval_seconds,
            "ACCESS_CACHE_SECONDS": self.access_cache_seconds,
            "ACCESS_DB_GRACE_SECONDS": self.access_db_grace_seconds,
            "CLIENT_HEARTBEAT_INTERVAL_SECONDS": self.client_heartbeat_interval_seconds,
            "WS_WATCHDOG_TIMEOUT_SECONDS": self.ws_watchdog_timeout_seconds,
            "PRESENCE_EXPIRY_SECONDS": self.presence_expiry_seconds,
            "PRESENCE_TICK_SECONDS": self.presence_tick_seconds,
            "PRESENCE_HINT_MIN_INTERVAL_SECONDS": self.presence_hint_min_interval_seconds,
            "TYPING_EXPIRY_SECONDS": self.typing_expiry_seconds,
            "TYPING_STATE_MIN_INTERVAL_SECONDS": self.typing_state_min_interval_seconds,
        }
        problems += [
            f"{name} must be greater than 0." for name, value in positives.items() if value <= 0
        ]
        if self.outbox_processed_retention_days < 0:
            problems.append("OUTBOX_PROCESSED_RETENTION_DAYS must be 0 or more.")
        if self.outbox_claim_lease_seconds < 2 * self.outbox_publish_timeout_seconds:
            problems.append(
                "OUTBOX_CLAIM_LEASE_SECONDS must be at least twice OUTBOX_PUBLISH_TIMEOUT_SECONDS "
                "so a claim cannot expire while its publish is still in flight."
            )
        problems += self._validate_part5()
        return problems

    def _validate_part6(self) -> list[str]:
        """Document limits must be positive and consistent with each other."""
        problems: list[str] = []
        for name, value in (
            ("DOCUMENT_MAX_SERIALIZED_BYTES", self.document_max_serialized_bytes),
            ("DOCUMENT_MAX_TEXT_CODEPOINTS", self.document_max_text_codepoints),
            ("DOCUMENT_MAX_TOTAL_NODES", self.document_max_total_nodes),
            ("DOCUMENT_MAX_BLOCKS", self.document_max_blocks),
            ("DOCUMENT_MAX_INLINE_NODES", self.document_max_inline_nodes),
            ("DOCUMENT_MAX_IMAGES", self.document_max_images),
            ("DOCUMENT_MAX_LINK_LENGTH", self.document_max_link_length),
            ("MAX_DOCUMENTS_PER_ROOM", self.max_documents_per_room),
            ("DOCUMENT_WRITES_PER_MINUTE", self.document_writes_per_minute),
        ):
            if value < 1:
                problems.append(f"{name} must be at least 1.")
        if not 1 <= self.document_max_list_depth <= 3:
            problems.append("DOCUMENT_MAX_LIST_DEPTH must be between 1 and 3.")
        if self.document_save_body_limit_bytes < self.document_max_serialized_bytes:
            problems.append(
                "DOCUMENT_SAVE_BODY_LIMIT_BYTES must be at least DOCUMENT_MAX_SERIALIZED_BYTES, "
                "or a maximum-size document could never be saved."
            )
        if not self.document_font_keys or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) for key in self.document_font_keys
        ):
            problems.append(
                "DOCUMENT_FONT_KEYS must be a comma-separated list of lowercase keys "
                "(letters, digits, underscore)."
            )
        return problems

    def _validate_part5(self) -> list[str]:
        """Relationships between the Part 5 timers; each protects a specific failure mode."""
        problems: list[str] = []
        if self.ws_watchdog_timeout_seconds < 2 * self.client_heartbeat_interval_seconds:
            problems.append(
                "WS_WATCHDOG_TIMEOUT_SECONDS must be at least twice "
                "CLIENT_HEARTBEAT_INTERVAL_SECONDS, or one late heartbeat disconnects a client."
            )
        if self.ack_timeout_seconds <= self.ws_send_timeout_seconds:
            problems.append(
                "ACK_TIMEOUT_SECONDS must exceed WS_SEND_TIMEOUT_SECONDS: a slow send is judged "
                "first and a stalled-ack close would otherwise hide it."
            )
        if self.replay_page_size > self.replay_max_events:
            problems.append("REPLAY_PAGE_SIZE must not exceed REPLAY_MAX_EVENTS.")
        if self.ack_window < 1 or self.ws_connection_queue_size < 2:
            problems.append("ACK_WINDOW and WS_CONNECTION_QUEUE_SIZE must be positive.")
        if self.presence_expiry_seconds < 2 * self.client_heartbeat_interval_seconds:
            problems.append(
                "PRESENCE_EXPIRY_SECONDS must be at least twice CLIENT_HEARTBEAT_INTERVAL_SECONDS."
            )
        if self.access_cache_seconds >= self.access_check_interval_seconds:
            problems.append(
                "ACCESS_CACHE_SECONDS must be smaller than ACCESS_CHECK_INTERVAL_SECONDS."
            )
        if self.reconcile_interval_degraded_seconds > self.reconcile_interval_seconds:
            problems.append(
                "RECONCILE_INTERVAL_DEGRADED_SECONDS must not exceed RECONCILE_INTERVAL_SECONDS."
            )
        return problems

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


class _CleanupSettings(BaseSettings):
    """What the maintenance commands need: the database and the outbox retention."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )
    database_url: SecretStr
    outbox_processed_retention_days: int = 7


def load_cleanup_settings() -> tuple[str, int]:
    """(database_url, outbox_processed_retention_days) without requiring the other settings."""
    try:
        loaded = _CleanupSettings()  # type: ignore[call-arg]
    except ValidationError:
        raise ConfigError("DATABASE_URL: must be set (see .env.example).") from None
    if loaded.outbox_processed_retention_days < 0:
        raise ConfigError("OUTBOX_PROCESSED_RETENTION_DAYS must be 0 or more.")
    return loaded.database_url.get_secret_value(), loaded.outbox_processed_retention_days


def load_database_url() -> str:
    try:
        return _DatabaseOnlySettings().database_url.get_secret_value()  # type: ignore[call-arg]
    except ValidationError:
        raise ConfigError("DATABASE_URL: must be set (see .env.example).") from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
