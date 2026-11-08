"""Builders for Settings used by tests. Never reads .env, so tests are hermetic."""

from __future__ import annotations

import os
import secrets

from app.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://threadforge:threadforge-dev-password@127.0.0.1:5442/threadforge_test",
)

TEST_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
TEST_CLIENT_SECRET = "test-client-secret-value"
TEST_CSRF_SECRET = "test-csrf-secret-0123456789-abcdefghijklmnop"
TEST_FINGERPRINT_SECRET = "test-fingerprint-secret-0123456789-abcdefghij"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": TEST_DATABASE_URL,
        "google_client_id": TEST_CLIENT_ID,
        "google_client_secret": TEST_CLIENT_SECRET,
        "public_base_url": "http://localhost:8000",
        "csrf_secret": TEST_CSRF_SECRET,
        "message_fingerprint_secret": TEST_FINGERPRINT_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def make_production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "google_client_id": "1234567890-abcdef.apps.googleusercontent.com",
        "google_client_secret": secrets.token_urlsafe(24),
        "public_base_url": "https://api.threadforge.test",
        "oidc_callback_url": "https://api.threadforge.test/auth/callback",
        "frontend_success_url": "https://app.threadforge.test/",
        "frontend_error_url": "https://app.threadforge.test/login-error",
        "trusted_origins": "https://app.threadforge.test",
        "frontend_base_url": "https://app.threadforge.test",
        "csrf_secret": secrets.token_urlsafe(48),
        "message_fingerprint_secret": secrets.token_urlsafe(48),
        "redis_url": f"rediss://:{secrets.token_urlsafe(24)}@redis.threadforge.test:6380/0",
    }
    values.update(overrides)
    return make_settings(**values)
