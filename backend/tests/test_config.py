import pytest
from pydantic import ValidationError

from app.config import ConfigError, Settings, load_settings
from tests.factories import make_production_settings, make_settings


def test_dev_defaults_are_visible_and_local():
    s = make_settings()
    assert s.frontend_success_url == "http://localhost:8000/auth/me"
    assert s.oidc_callback_url == "http://localhost:8000/auth/callback"
    assert s.trusted_origins == ["http://localhost:8000"]
    assert s.session_cookie_name == "tf_session"
    assert s.secure_cookies is False


def test_production_cookies_are_secure_and_host_prefixed():
    s = make_production_settings()
    assert s.secure_cookies is True
    assert s.session_cookie_name == "__Host-tf_session"
    assert s.binding_cookie_name == "__Host-tf_login_binding"


def test_production_rejects_insecure_cookie_flag():
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        make_production_settings(cookie_secure=False)


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"csrf_secret": "short"}, "CSRF_SECRET"),
        ({"message_fingerprint_secret": "short"}, "MESSAGE_FINGERPRINT_SECRET"),
        ({"message_fingerprint_secret": "replace-me-" + "x" * 40}, "MESSAGE_FINGERPRINT_SECRET"),
        ({"message_writes_per_minute": 0}, "MESSAGE_WRITES_PER_MINUTE"),
        ({"csrf_secret": "replace-me-" + "x" * 40}, "CSRF_SECRET"),
        ({"public_base_url": "http://api.threadforge.test"}, "https"),
        ({"google_client_id": ""}, "GOOGLE_CLIENT_ID"),
        ({"google_client_secret": "replace-me"}, "GOOGLE_CLIENT_ID"),
        ({"frontend_success_url": ""}, "FRONTEND_SUCCESS_URL"),
        ({"frontend_error_url": ""}, "FRONTEND_ERROR_URL"),
        ({"trusted_origins": ""}, "TRUSTED_ORIGINS"),
        ({"trusted_origins": "*"}, "TRUSTED_ORIGINS"),
        ({"oidc_callback_url": "https://evil.test/auth/callback"}, "same origin"),
        ({"oidc_callback_url": "https://api.threadforge.test/other"}, "/auth/callback"),
    ],
)
def test_production_validation_failures(override, fragment):
    with pytest.raises(ValidationError, match=fragment):
        make_production_settings(**override)


def test_plain_http_only_allowed_for_localhost_in_development():
    with pytest.raises(ValidationError, match="localhost"):
        make_settings(public_base_url="http://example.org")


def test_database_url_must_be_asyncpg():
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        make_settings(database_url="postgresql://u:p@localhost/x")


def test_unconfigured_google_is_allowed_in_development():
    s = make_settings(google_client_id="", google_client_secret="")
    assert s.oidc_configured is False


def test_repr_never_contains_secrets():
    s = make_settings(
        google_client_secret="topsecret-google", csrf_secret="topsecret-csrf-0123456789abcdefghij"
    )
    text = repr(s) + str(s)
    assert "topsecret" not in text
    assert "threadforge-dev-password" not in text


def test_config_error_message_has_no_secret_values(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:SUPERSECRETPW@h/db")
    monkeypatch.setenv("CSRF_SECRET", "tiny-SUPERSECRETCSRF")
    monkeypatch.setenv("MESSAGE_FINGERPRINT_SECRET", "tiny-SUPERSECRETFP")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "SUPERSECRET" not in str(info.value)
    assert "CSRF_SECRET" in str(info.value)
    assert "MESSAGE_FINGERPRINT_SECRET" in str(info.value)
    assert "SUPERSECRETFP" not in str(info.value)


def test_env_example_is_valid_for_development_and_unsafe_for_production():
    """The documented template works as-is locally, and cannot be deployed unchanged."""
    from app.config import REPO_ROOT

    template = REPO_ROOT / ".env.example"
    dev = Settings(_env_file=template)
    assert dev.app_env == "development"
    assert dev.oidc_configured is False  # placeholder Google credentials => login not configured
    assert dev.oidc_callback_url == "http://localhost:8000/auth/callback"
    assert dev.database_name == "threadforge"

    with pytest.raises(ValidationError):
        Settings(_env_file=template, app_env="production")
