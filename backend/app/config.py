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

    # Part 6: image assets and storage.
    storage_path: str = "./data/storage"  # shared by the API and the workers
    asset_max_upload_bytes: int = 10 * 1024 * 1024
    asset_max_pixels: int = 25_000_000
    asset_room_quota_bytes: int = 200 * 1024 * 1024
    asset_max_per_room: int = 1000
    asset_uploads_per_minute: int = 30  # per user per room
    asset_processing_concurrency: int = 2  # image decodes at once, per API process
    asset_pending_ttl_hours: int = 24  # a pending asset nobody saved is deleted after this
    asset_unreferenced_grace_days: int = 7  # an asset a save dropped is deleted after this

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

    # Collaboration (Tiptap + Yjs + Hocuspocus). The Node collaboration service talks to the
    # /internal/collab/* routes with this shared secret. Empty = the internal routes answer 503.
    internal_api_secret: SecretStr = SecretStr("")
    # Where the browser reaches the collaboration WebSocket (same origin; a proxy path)
    collab_public_path: str = "/collab"
    collab_lease_ttl_seconds: int = 30  # the service renews every 10 s
    collab_max_state_bytes: int = 8 * 1024 * 1024  # a larger Yjs state is refused with 413
    collab_snapshots_kept: int = 10  # newest N per document, plus the newest seed

    # ThreadForge AI (phase 1). EVERYTHING about providers is configuration; nothing is hardcoded.
    # AI stays off until AI_ENABLED=true AND the model settings below are filled in and valid.
    ai_enabled: bool = False
    ai_key_policy: Literal["server_only", "user_only", "user_if_present"] = "user_if_present"
    ai_openai_api_key: SecretStr = SecretStr("")  # the server's key (workers only)
    ai_google_api_key: SecretStr = SecretStr("")
    ai_openai_base_url: str = "https://api.openai.com"
    ai_google_base_url: str = "https://generativelanguage.googleapis.com"
    # key_id:base64(32 bytes) pairs, comma separated; the FIRST encrypts (not the Drive keys)
    ai_key_encryption_keys: SecretStr = SecretStr("")
    # generation model
    ai_llm_model: str = ""
    ai_llm_context_tokens: int = 0
    ai_llm_max_output_tokens: int = 0
    ai_llm_structured_output: bool = True  # False = parse plus one bounded repair
    ai_llm_reasoning_effort: str = ""  # empty = do not send the parameter
    # embedding model
    ai_embed_model: str = ""
    ai_embed_dimensions: int = 0
    ai_embed_max_input_tokens: int = 0
    ai_embed_batch_size: int = 32
    ai_embed_normalize: bool = False  # the documented model normalizes shortened vectors itself
    ai_embed_query_prefix: str = "task: search result | query: "
    ai_embed_document_prefix: str = "title: none | text: "
    # prices: integer micro-dollars per MILLION tokens (USD 0.10 per 1M tokens = 100000)
    ai_llm_input_micros_per_mtok: int | None = None
    ai_llm_output_micros_per_mtok: int | None = None
    ai_embed_text_micros_per_mtok: int | None = None
    ai_embed_image_micros_per_mtok: int | None = None
    ai_prices_checked_on: str = ""  # YYYY-MM-DD; required when any price is set
    ai_image_tokens_estimate: int = 1500  # conservative per-image input tokens for the generator
    ai_embed_image_tokens_estimate: int = 258  # the embedding docs: a visual page counts as 258
    # token estimation (no verified local tokenizer): characters / chars_per_token * safety_margin
    ai_chars_per_token: float = 3.0
    ai_token_safety_margin: float = 1.1
    # budgets, integer micro-dollars; unset = not enforced. Server-key calls only.
    ai_budget_global_monthly_micros: int | None = None
    ai_budget_user_daily_micros: int | None = None
    ai_budget_room_daily_micros: int | None = None
    # per-request caps, enforced before anything is sent (also for user keys)
    ai_max_input_tokens: int = 200_000
    ai_max_output_tokens: int = 8_000
    ai_max_images: int = 8
    ai_max_provider_calls: int = 40
    ai_max_new_analysis_pages: int = 100
    ai_max_new_analysis_figures: int = 40
    ai_confirm_threshold_micros: int | None = None  # estimate above this needs confirm_large
    ai_confirm_threshold_tokens: int = 100_000  # used when prices are unset
    # call policy
    ai_call_timeout_seconds: float = 120.0
    ai_retry_max_attempts: int = 4
    ai_retry_base_delay_seconds: float = 1.0
    ai_retry_max_delay_seconds: float = 30.0
    ai_key_concurrency: int = 4  # simultaneous provider calls per key, per worker process
    ai_key_validations_per_user_per_hour: int = 10
    ai_key_validations_per_ip_per_hour: int = 30
    ai_selfcheck_interval_seconds: int = 3600

    # AI checkpoint 2: sources (files attached to AI requests). Independent of AI_ENABLED: a file
    # can be uploaded and inspected locally with AI off; nothing leaves the server at upload.
    source_max_upload_bytes: int = 25 * 1024 * 1024
    source_max_per_room: int = 100  # active sources
    source_room_quota_bytes: int = 500 * 1024 * 1024  # all live versions, plus uploads in flight
    source_uploads_per_minute: int = 10  # per user per room (intents created)
    source_max_open_uploads_per_user: int = 3  # started but not finished, per user per room
    source_upload_ttl_minutes: int = 60  # an unfinished upload is abandoned after this
    source_download_roles: Annotated[list[str], NoDecode] = ["owner", "editor", "viewer"]
    source_max_pages: int = 400  # PDFs with more pages are refused at upload
    source_inspect_timeout_seconds: int = 30  # wall clock for the sandboxed inspection
    source_inspect_memory_mb: int = 1024  # address-space limit of the child (POSIX only)
    source_inspect_concurrency: int = 2  # inspections at once, per API process
    source_docx_max_uncompressed_bytes: int = 200 * 1024 * 1024
    source_docx_max_entries: int = 5000

    # AI checkpoint 3: lazy analysis (extraction, OCR, figures, chunking, embeddings) and retrieval.
    # Analysis runs only in the AI worker, only when a source version is attached to a request.
    ai_tesseract_command: str = (
        "tesseract"  # looked up on PATH by the PARENT; the child gets a path
    )
    ai_ocr_dpi: int = 300  # page render resolution for OCR
    ai_ocr_min_confidence: int = 60  # mean word confidence (0-100) below this = the page failed OCR
    ai_ocr_min_words_per_page: int = 12  # fewer words than this on a mostly-text page = failed
    ai_ocr_max_implausible_share: float = 0.5  # share of non-word-like tokens above this = failed
    ai_ocr_timeout_seconds: int = 60  # per page, Tesseract
    ai_ocr_max_pages_per_source: int = 100
    ai_vision_fallback_max_pages: int = 20  # pages sent to the vision model after failed OCR
    ai_unreadable_fail_share: float = 0.3  # more unreadable pages than this = the version fails
    ai_max_figures_per_source: int = 40
    ai_figure_embedding_mode: Literal["image", "description", "image_plus_description"] = (
        "description"
    )
    ai_figure_min_pixels: int = 10_000  # smaller images are decorative
    ai_figure_max_repeats: int = 3  # the same image on more pages than this is a logo/decoration
    ai_chunk_target_tokens: int = 400
    ai_chunk_max_tokens: int = 900  # hard maximum; must stay under AI_EMBED_MAX_INPUT_TOKENS
    ai_chunk_overlap_tokens: int = 40
    ai_max_chunks_per_source: int = 2000
    ai_max_extracted_chars: int = 5_000_000
    ai_extract_timeout_seconds: int = 600  # wall clock for the whole sandboxed extraction
    ai_extract_memory_mb: int = 2048  # address-space limit of the extractor child (POSIX only)
    ai_analysis_max_provider_calls: int = 600  # per analysis job (embedding batches, vision calls)
    ai_analysis_lease_seconds: int = 120  # a claimed job not renewed within this is up for grabs
    ai_analysis_max_attempts: int = 3
    ai_worker_concurrency: int = 2  # jobs at once, per worker process
    ai_worker_poll_seconds: float = 2.0
    ai_retrieval_lexical_k: int = 20
    ai_retrieval_vector_k: int = 20
    ai_retrieval_rrf_k: int = 60
    ai_retrieval_evidence_tokens: int = 24_000  # the evidence budget one request may fill
    ai_retrieval_figure_top_k: int = 5  # figure images attached to the answer call
    ai_retrieval_min_source_share: float = 0.2  # each selected source gets at least this share
    # a vector hit farther than this cosine distance is not 'relevant' (0 = identical, 2 = opposite)
    ai_retrieval_max_distance: float = 0.6
    ai_superseded_generation_grace_hours: int = (
        24  # an old index outlives its replacement this long
    )

    # AI checkpoint 4: `@AI` requests, answers, document capture, Mermaid.
    ai_request_ttl_minutes: int = 30  # waiting/queued longer than this = expired
    ai_max_active_requests_per_user: int = 3
    ai_max_active_requests_per_room: int = 10
    ai_request_max_chat_context: int = 20
    ai_request_max_sources: int = 10
    ai_request_lease_seconds: int = 120
    ai_request_max_attempts: int = 3  # the requester may retry a failed request this many times
    ai_result_retention_days: int = 30  # manifests older than this are deleted by cleanup_ai
    ai_summarize_section_tokens: int = 3000  # map-step input budget per call
    ai_summarize_source_tokens: int = 60_000  # per-source input budget before sampling kicks in
    ai_collab_timeout_seconds: float = 20.0
    ai_node_command: str = "node"
    ai_mermaid_check_script: str = (
        ""  # empty = packages/mermaid-check/dist/cli.js in the repository
    )
    ai_mermaid_check_timeout_seconds: float = 25.0
    # AI phase 2: edit proposals, drafts, applying to the live document.
    ai_proposals_enabled: bool = False  # the composer offers edit/draft only when this is on
    ai_proposal_ttl_days: int = 7  # a pending proposal expires after this
    ai_proposal_retention_days: int = 30  # a resolved proposal is deleted this long after
    ai_proposals_per_user_per_hour: int = 20  # edit/draft requests per user
    ai_proposal_check_script: str = ""  # empty = packages/proposal-check/dist/cli.js
    ai_proposal_check_timeout_seconds: float = 25.0
    ai_diagram_split_chars: int = 8000  # a longer generated diagram fails with "split it"
    ai_apply_quiet_seconds: float = 3.0  # apply waits for no human edit within this window
    ai_apply_max_wait_seconds: float = 10.0  # ...for at most this long, then document_busy
    collab_service_url: str = (
        "http://127.0.0.1:4010"  # the private address of the collaboration service
    )

    trusted_proxy_ips: Annotated[list[str], NoDecode] = []
    cookie_secure: bool | None = None

    @field_validator(
        "trusted_origins",
        "trusted_proxy_ips",
        "document_font_keys",
        "source_download_roles",
        mode="before",
    )
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
    def ai_prices_configured(self) -> bool:
        """Spend can be computed only when generation AND the text-embedding prices are known."""
        return (
            self.ai_llm_input_micros_per_mtok is not None
            and self.ai_llm_output_micros_per_mtok is not None
            and self.ai_embed_text_micros_per_mtok is not None
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
        problems += self._validate_collab(prod)
        problems += self._validate_ai(prod)
        problems += self._validate_sources()
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
        ):
            if value < 1:
                problems.append(f"{name} must be at least 1.")
        for name, value in (
            ("ASSET_MAX_UPLOAD_BYTES", self.asset_max_upload_bytes),
            ("ASSET_MAX_PIXELS", self.asset_max_pixels),
            ("ASSET_ROOM_QUOTA_BYTES", self.asset_room_quota_bytes),
            ("ASSET_MAX_PER_ROOM", self.asset_max_per_room),
            ("ASSET_UPLOADS_PER_MINUTE", self.asset_uploads_per_minute),
            ("ASSET_PROCESSING_CONCURRENCY", self.asset_processing_concurrency),
            ("ASSET_PENDING_TTL_HOURS", self.asset_pending_ttl_hours),
            ("ASSET_UNREFERENCED_GRACE_DAYS", self.asset_unreferenced_grace_days),
        ):
            if value < 1:
                problems.append(f"{name} must be at least 1.")
        if self.asset_room_quota_bytes < self.asset_max_upload_bytes:
            problems.append("ASSET_ROOM_QUOTA_BYTES must be at least ASSET_MAX_UPLOAD_BYTES.")
        if not self.storage_path.strip():
            problems.append("STORAGE_PATH must be set.")
        if not 1 <= self.document_max_list_depth <= 3:
            problems.append("DOCUMENT_MAX_LIST_DEPTH must be between 1 and 3.")
        if not self.document_font_keys or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) for key in self.document_font_keys
        ):
            problems.append(
                "DOCUMENT_FONT_KEYS must be a comma-separated list of lowercase keys "
                "(letters, digits, underscore)."
            )
        return problems

    def _validate_collab(self, prod: bool) -> list[str]:
        problems: list[str] = []
        secret = self.internal_api_secret.get_secret_value()
        if prod and (len(secret) < MIN_PRODUCTION_SECRET_LENGTH or is_placeholder(secret)):
            problems.append(
                "INTERNAL_API_SECRET must be a real random value of at least "
                f"{MIN_PRODUCTION_SECRET_LENGTH} characters in production "
                "(the collaboration service authenticates to /internal/collab with it)."
            )
        elif secret and len(secret) < 16:
            problems.append("INTERNAL_API_SECRET must be at least 16 characters when set.")
        if not re.fullmatch(r"/[A-Za-z0-9._/-]{1,64}", self.collab_public_path):
            problems.append("COLLAB_PUBLIC_PATH must be an absolute path like /collab.")
        if not 10 <= self.collab_lease_ttl_seconds <= 600:
            problems.append("COLLAB_LEASE_TTL_SECONDS must be between 10 and 600.")
        if self.collab_max_state_bytes < 64 * 1024:
            problems.append("COLLAB_MAX_STATE_BYTES must be at least 65536.")
        if self.collab_snapshots_kept < 1:
            problems.append("COLLAB_SNAPSHOTS_KEPT must be at least 1.")
        return problems

    def _validate_sources(self) -> list[str]:
        problems = [
            f"{name} must be at least 1."
            for name, value in (
                ("SOURCE_MAX_UPLOAD_BYTES", self.source_max_upload_bytes),
                ("SOURCE_MAX_PER_ROOM", self.source_max_per_room),
                ("SOURCE_ROOM_QUOTA_BYTES", self.source_room_quota_bytes),
                ("SOURCE_UPLOADS_PER_MINUTE", self.source_uploads_per_minute),
                ("SOURCE_MAX_OPEN_UPLOADS_PER_USER", self.source_max_open_uploads_per_user),
                ("SOURCE_UPLOAD_TTL_MINUTES", self.source_upload_ttl_minutes),
                ("SOURCE_MAX_PAGES", self.source_max_pages),
                ("SOURCE_INSPECT_TIMEOUT_SECONDS", self.source_inspect_timeout_seconds),
                ("SOURCE_INSPECT_MEMORY_MB", self.source_inspect_memory_mb),
                ("SOURCE_INSPECT_CONCURRENCY", self.source_inspect_concurrency),
                ("SOURCE_DOCX_MAX_UNCOMPRESSED_BYTES", self.source_docx_max_uncompressed_bytes),
                ("SOURCE_DOCX_MAX_ENTRIES", self.source_docx_max_entries),
            )
            if value < 1
        ]
        if self.source_room_quota_bytes < self.source_max_upload_bytes:
            problems.append("SOURCE_ROOM_QUOTA_BYTES must be at least SOURCE_MAX_UPLOAD_BYTES.")
        if self.source_inspect_memory_mb < 256:
            problems.append(
                "SOURCE_INSPECT_MEMORY_MB must be at least 256 (PDF rendering needs it)."
            )
        roles = set(self.source_download_roles)
        if not roles or not roles <= {"owner", "editor", "viewer"}:
            problems.append(
                "SOURCE_DOWNLOAD_ROLES must be a comma-separated list of owner, editor, viewer."
            )
        elif "owner" not in roles:
            problems.append("SOURCE_DOWNLOAD_ROLES must include owner.")
        return problems

    def _validate_ai(self, prod: bool) -> list[str]:
        """AI settings. Anything missing leaves AI off (AI_ENABLED=false needs none of this); an
        enabled-but-incomplete configuration is a startup error that names the setting."""
        problems: list[str] = []
        prices = (
            self.ai_llm_input_micros_per_mtok,
            self.ai_llm_output_micros_per_mtok,
            self.ai_embed_text_micros_per_mtok,
            self.ai_embed_image_micros_per_mtok,
        )
        budgets = (
            self.ai_budget_global_monthly_micros,
            self.ai_budget_user_daily_micros,
            self.ai_budget_room_daily_micros,
        )
        for name, value in (
            ("AI_LLM_INPUT_MICROS_PER_MTOK", prices[0]),
            ("AI_LLM_OUTPUT_MICROS_PER_MTOK", prices[1]),
            ("AI_EMBED_TEXT_MICROS_PER_MTOK", prices[2]),
            ("AI_EMBED_IMAGE_MICROS_PER_MTOK", prices[3]),
            ("AI_BUDGET_GLOBAL_MONTHLY_MICROS", budgets[0]),
            ("AI_BUDGET_USER_DAILY_MICROS", budgets[1]),
            ("AI_BUDGET_ROOM_DAILY_MICROS", budgets[2]),
            ("AI_CONFIRM_THRESHOLD_MICROS", self.ai_confirm_threshold_micros),
        ):
            if value is not None and value < 0:
                problems.append(f"{name} must be 0 or more.")
        if any(p is not None for p in prices) and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", self.ai_prices_checked_on
        ):
            problems.append(
                "AI_PRICES_CHECKED_ON (YYYY-MM-DD) must be set when any AI price is set: "
                "prices go stale, and the date says when they were last checked."
            )
        if any(b is not None for b in budgets) and not self.ai_prices_configured:
            problems.append(
                "AI budgets need the prices (AI_LLM_INPUT_MICROS_PER_MTOK, "
                "AI_LLM_OUTPUT_MICROS_PER_MTOK, AI_EMBED_TEXT_MICROS_PER_MTOK): a budget cannot "
                "be enforced without prices."
            )
        for name, positive in (
            ("AI_MAX_INPUT_TOKENS", self.ai_max_input_tokens),
            ("AI_MAX_OUTPUT_TOKENS", self.ai_max_output_tokens),
            ("AI_MAX_IMAGES", self.ai_max_images + 1),  # 0 images is allowed
            ("AI_MAX_PROVIDER_CALLS", self.ai_max_provider_calls),
            ("AI_MAX_NEW_ANALYSIS_PAGES", self.ai_max_new_analysis_pages),
            ("AI_MAX_NEW_ANALYSIS_FIGURES", self.ai_max_new_analysis_figures + 1),
            ("AI_CONFIRM_THRESHOLD_TOKENS", self.ai_confirm_threshold_tokens),
            ("AI_EMBED_BATCH_SIZE", self.ai_embed_batch_size),
            ("AI_RETRY_MAX_ATTEMPTS", self.ai_retry_max_attempts),
            ("AI_KEY_CONCURRENCY", self.ai_key_concurrency),
            ("AI_KEY_VALIDATIONS_PER_USER_PER_HOUR", self.ai_key_validations_per_user_per_hour),
            ("AI_KEY_VALIDATIONS_PER_IP_PER_HOUR", self.ai_key_validations_per_ip_per_hour),
            ("AI_SELFCHECK_INTERVAL_SECONDS", self.ai_selfcheck_interval_seconds),
        ):
            if positive < 1:
                problems.append(f"{name} must be at least 1.")
        if self.ai_chars_per_token <= 0 or self.ai_token_safety_margin < 1:
            problems.append(
                "AI_CHARS_PER_TOKEN must be positive and AI_TOKEN_SAFETY_MARGIN at least 1."
            )
        if self.ai_call_timeout_seconds <= 0 or self.ai_retry_base_delay_seconds <= 0:
            problems.append("AI timeouts and retry delays must be greater than 0.")
        if self.ai_retry_max_delay_seconds < self.ai_retry_base_delay_seconds:
            problems.append("AI_RETRY_MAX_DELAY_SECONDS must not be below the base delay.")
        for name, url in (
            ("AI_OPENAI_BASE_URL", self.ai_openai_base_url),
            ("AI_GOOGLE_BASE_URL", self.ai_google_base_url),
        ):
            parts = urlsplit(url)
            if parts.scheme != "https" and (parts.hostname or "") not in _LOCAL_HOSTS:
                problems.append(f"{name} must be an https URL (http only for localhost tests).")
        if self.ai_key_encryption_keys.get_secret_value():
            from app.ai.crypto import KeySet

            try:
                KeySet.parse(self.ai_key_encryption_keys.get_secret_value())
            except ValueError as exc:
                problems.append(f"AI_KEY_ENCRYPTION_KEYS: {exc}")
        for name, positive in (
            ("AI_REQUEST_TTL_MINUTES", self.ai_request_ttl_minutes),
            ("AI_MAX_ACTIVE_REQUESTS_PER_USER", self.ai_max_active_requests_per_user),
            ("AI_MAX_ACTIVE_REQUESTS_PER_ROOM", self.ai_max_active_requests_per_room),
            ("AI_REQUEST_MAX_CHAT_CONTEXT", self.ai_request_max_chat_context),
            ("AI_REQUEST_MAX_SOURCES", self.ai_request_max_sources),
            ("AI_REQUEST_LEASE_SECONDS", self.ai_request_lease_seconds),
            ("AI_REQUEST_MAX_ATTEMPTS", self.ai_request_max_attempts),
            ("AI_RESULT_RETENTION_DAYS", self.ai_result_retention_days),
            ("AI_SUMMARIZE_SECTION_TOKENS", self.ai_summarize_section_tokens),
            ("AI_SUMMARIZE_SOURCE_TOKENS", self.ai_summarize_source_tokens),
        ):
            if positive < 1:
                problems.append(f"{name} must be at least 1.")
        if self.ai_request_max_attempts > 10:
            problems.append("AI_REQUEST_MAX_ATTEMPTS must be at most 10.")
        if self.ai_request_max_sources > 10:
            problems.append("AI_REQUEST_MAX_SOURCES must be at most 10.")
        if self.ai_request_max_chat_context > 20:
            problems.append("AI_REQUEST_MAX_CHAT_CONTEXT must be at most 20.")
        for name, positive in (
            ("AI_PROPOSAL_TTL_DAYS", self.ai_proposal_ttl_days),
            ("AI_PROPOSAL_RETENTION_DAYS", self.ai_proposal_retention_days),
            ("AI_PROPOSALS_PER_USER_PER_HOUR", self.ai_proposals_per_user_per_hour),
            ("AI_DIAGRAM_SPLIT_CHARS", self.ai_diagram_split_chars),
        ):
            if positive < 1:
                problems.append(f"{name} must be at least 1.")
        if self.ai_diagram_split_chars > 20_000:
            problems.append("AI_DIAGRAM_SPLIT_CHARS must be at most 20000 (the Mermaid limit).")
        if self.ai_proposal_check_timeout_seconds <= 0 or self.ai_apply_quiet_seconds < 0:
            problems.append(
                "AI_PROPOSAL_CHECK_TIMEOUT_SECONDS must be positive and "
                "AI_APPLY_QUIET_SECONDS must not be negative."
            )
        if self.ai_apply_max_wait_seconds < self.ai_apply_quiet_seconds:
            problems.append("AI_APPLY_MAX_WAIT_SECONDS must be at least AI_APPLY_QUIET_SECONDS.")
        if self.ai_collab_timeout_seconds <= 0 or self.ai_mermaid_check_timeout_seconds <= 0:
            problems.append(
                "AI_COLLAB_TIMEOUT_SECONDS and AI_MERMAID_CHECK_TIMEOUT_SECONDS must be positive."
            )
        for name, positive in (
            ("AI_OCR_DPI", self.ai_ocr_dpi),
            ("AI_OCR_TIMEOUT_SECONDS", self.ai_ocr_timeout_seconds),
            ("AI_OCR_MAX_PAGES_PER_SOURCE", self.ai_ocr_max_pages_per_source),
            ("AI_CHUNK_TARGET_TOKENS", self.ai_chunk_target_tokens),
            ("AI_CHUNK_MAX_TOKENS", self.ai_chunk_max_tokens),
            ("AI_MAX_CHUNKS_PER_SOURCE", self.ai_max_chunks_per_source),
            ("AI_MAX_EXTRACTED_CHARS", self.ai_max_extracted_chars),
            ("AI_EXTRACT_TIMEOUT_SECONDS", self.ai_extract_timeout_seconds),
            ("AI_EXTRACT_MEMORY_MB", self.ai_extract_memory_mb),
            ("AI_ANALYSIS_MAX_PROVIDER_CALLS", self.ai_analysis_max_provider_calls),
            ("AI_ANALYSIS_LEASE_SECONDS", self.ai_analysis_lease_seconds),
            ("AI_ANALYSIS_MAX_ATTEMPTS", self.ai_analysis_max_attempts),
            ("AI_WORKER_CONCURRENCY", self.ai_worker_concurrency),
            ("AI_RETRIEVAL_LEXICAL_K", self.ai_retrieval_lexical_k),
            ("AI_RETRIEVAL_VECTOR_K", self.ai_retrieval_vector_k),
            ("AI_RETRIEVAL_RRF_K", self.ai_retrieval_rrf_k),
            ("AI_RETRIEVAL_EVIDENCE_TOKENS", self.ai_retrieval_evidence_tokens),
        ):
            if positive < 1:
                problems.append(f"{name} must be at least 1.")
        if not 0 <= self.ai_ocr_min_confidence <= 100:
            problems.append("AI_OCR_MIN_CONFIDENCE must be between 0 and 100.")
        if not 0 < self.ai_unreadable_fail_share <= 1:
            problems.append("AI_UNREADABLE_FAIL_SHARE must be greater than 0 and at most 1.")
        if not 0 < self.ai_ocr_max_implausible_share <= 1:
            problems.append("AI_OCR_MAX_IMPLAUSIBLE_SHARE must be greater than 0 and at most 1.")
        if not 0 < self.ai_retrieval_max_distance <= 2:
            problems.append("AI_RETRIEVAL_MAX_DISTANCE must be greater than 0 and at most 2.")
        if self.ai_superseded_generation_grace_hours < 0:
            problems.append("AI_SUPERSEDED_GENERATION_GRACE_HOURS must not be negative.")
        if not 0 <= self.ai_retrieval_min_source_share <= 0.5:
            problems.append("AI_RETRIEVAL_MIN_SOURCE_SHARE must be between 0 and 0.5.")
        if self.ai_chunk_max_tokens < self.ai_chunk_target_tokens:
            problems.append("AI_CHUNK_MAX_TOKENS must not be below AI_CHUNK_TARGET_TOKENS.")
        if (
            self.ai_chunk_overlap_tokens < 0
            or self.ai_chunk_overlap_tokens >= self.ai_chunk_target_tokens
        ):
            problems.append("AI_CHUNK_OVERLAP_TOKENS must be at least 0 and below the target.")
        if self.ai_vision_fallback_max_pages < 0 or self.ai_max_figures_per_source < 0:
            problems.append(
                "AI_VISION_FALLBACK_MAX_PAGES and AI_MAX_FIGURES_PER_SOURCE must not be negative."
            )
        if not self.ai_enabled:
            return problems

        for name, value in (
            ("AI_LLM_MODEL", self.ai_llm_model),
            ("AI_EMBED_MODEL", self.ai_embed_model),
        ):
            if not value.strip():
                problems.append(f"{name} must be set when AI_ENABLED=true (see docs/ai.md).")
        for name, number in (
            ("AI_LLM_CONTEXT_TOKENS", self.ai_llm_context_tokens),
            ("AI_LLM_MAX_OUTPUT_TOKENS", self.ai_llm_max_output_tokens),
            ("AI_EMBED_DIMENSIONS", self.ai_embed_dimensions),
            ("AI_EMBED_MAX_INPUT_TOKENS", self.ai_embed_max_input_tokens),
        ):
            if number < 1:
                problems.append(f"{name} must be set (a positive number) when AI_ENABLED=true.")
        if (
            self.ai_embed_max_input_tokens
            and self.ai_chunk_max_tokens > self.ai_embed_max_input_tokens
        ):
            problems.append("AI_CHUNK_MAX_TOKENS must not exceed AI_EMBED_MAX_INPUT_TOKENS.")
        if (
            self.ai_llm_context_tokens
            and self.ai_max_input_tokens + self.ai_max_output_tokens > self.ai_llm_context_tokens
        ):
            problems.append(
                "AI_MAX_INPUT_TOKENS + AI_MAX_OUTPUT_TOKENS must fit in AI_LLM_CONTEXT_TOKENS."
            )
        if self.ai_llm_max_output_tokens and (
            self.ai_max_output_tokens > self.ai_llm_max_output_tokens
        ):
            problems.append("AI_MAX_OUTPUT_TOKENS must not exceed AI_LLM_MAX_OUTPUT_TOKENS.")
        has_server_keys = bool(
            self.ai_openai_api_key.get_secret_value() and self.ai_google_api_key.get_secret_value()
        )
        if self.ai_key_policy == "server_only" and not has_server_keys:
            problems.append(
                "AI_KEY_POLICY=server_only needs both AI_OPENAI_API_KEY and AI_GOOGLE_API_KEY."
            )
        if (
            self.ai_key_policy != "server_only"
            and not self.ai_key_encryption_keys.get_secret_value()
        ):
            problems.append(
                "AI_KEY_ENCRYPTION_KEYS must be set when users may bring their own keys "
                "(AI_KEY_POLICY is not server_only)."
            )
        if prod:
            for name, secret in (
                ("AI_OPENAI_API_KEY", self.ai_openai_api_key.get_secret_value()),
                ("AI_GOOGLE_API_KEY", self.ai_google_api_key.get_secret_value()),
            ):
                if secret and is_placeholder(secret):
                    problems.append(f"{name} still looks like a placeholder.")
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


class _DocumentCleanupSettings(BaseSettings):
    """What `cleanup_documents` needs: the database and the storage/lifecycle settings."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )
    database_url: SecretStr
    storage_path: str = "./data/storage"
    asset_pending_ttl_hours: int = 24
    asset_unreferenced_grace_days: int = 7


def load_document_cleanup_settings() -> _DocumentCleanupSettings:
    try:
        loaded = _DocumentCleanupSettings()  # type: ignore[call-arg]
    except ValidationError:
        raise ConfigError("DATABASE_URL: must be set (see .env.example).") from None
    if loaded.asset_pending_ttl_hours < 1 or loaded.asset_unreferenced_grace_days < 1:
        raise ConfigError(
            "ASSET_PENDING_TTL_HOURS and ASSET_UNREFERENCED_GRACE_DAYS must be at least 1."
        )
    return loaded


class _AiCleanupSettings(BaseSettings):
    """What `cleanup_ai` needs: the database and the storage path."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )
    database_url: SecretStr
    storage_path: str = "./data/storage"
    ai_superseded_generation_grace_hours: int = 24


def load_ai_cleanup_settings() -> _AiCleanupSettings:
    try:
        return _AiCleanupSettings()  # type: ignore[call-arg]
    except ValidationError:
        raise ConfigError("DATABASE_URL: must be set (see .env.example).") from None


class _MigrateSettings(BaseSettings):
    """What `migrate_documents_to_collab` needs: database, shared secret, service address."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )
    database_url: SecretStr
    internal_api_secret: SecretStr = SecretStr("")
    collab_service_url: str = "http://127.0.0.1:4010"


def load_migrate_settings() -> _MigrateSettings:
    try:
        loaded = _MigrateSettings()  # type: ignore[call-arg]
    except ValidationError:
        raise ConfigError("DATABASE_URL: must be set (see .env.example).") from None
    if len(loaded.internal_api_secret.get_secret_value()) < 16:
        raise ConfigError("INTERNAL_API_SECRET must be set (the same value as the service's).")
    if not loaded.collab_service_url.startswith(("http://", "https://")):
        raise ConfigError("COLLAB_SERVICE_URL must be an http(s) URL.")
    return loaded


def load_database_url() -> str:
    try:
        return _DatabaseOnlySettings().database_url.get_secret_value()  # type: ignore[call-arg]
    except ValidationError:
        raise ConfigError("DATABASE_URL: must be set (see .env.example).") from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
