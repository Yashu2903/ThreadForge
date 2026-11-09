"""SOURCE_* settings: defaults, validation messages that name the setting, the cleanup loader, and the
migration (expand only, working downgrade)."""

import asyncio

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import ConfigError, Settings, load_ai_cleanup_settings
from tests.db_utils import SOURCE_TABLES, alembic_config, build_fresh_schema, table_names
from tests.factories import TEST_DATABASE_URL, make_settings


def test_defaults_match_the_documented_values():
    s = make_settings()
    assert s.source_max_upload_bytes == 25 * 1024 * 1024
    assert (s.source_max_per_room, s.source_room_quota_bytes) == (100, 500 * 1024 * 1024)
    assert (s.source_uploads_per_minute, s.source_max_open_uploads_per_user) == (10, 3)
    assert (s.source_upload_ttl_minutes, s.source_max_pages) == (60, 400)
    assert s.source_download_roles == ["owner", "editor", "viewer"]
    assert (s.source_inspect_timeout_seconds, s.source_inspect_memory_mb) == (30, 1024)


def test_download_roles_are_read_from_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv("SOURCE_DOWNLOAD_ROLES", "owner, editor")
    assert make_settings().source_download_roles == ["owner", "editor"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("source_max_upload_bytes", 0),
        ("source_max_per_room", 0),
        ("source_room_quota_bytes", 0),
        ("source_uploads_per_minute", 0),
        ("source_max_open_uploads_per_user", 0),
        ("source_upload_ttl_minutes", 0),
        ("source_max_pages", 0),
        ("source_inspect_timeout_seconds", 0),
        ("source_inspect_memory_mb", 100),
        ("source_inspect_concurrency", 0),
        ("source_docx_max_uncompressed_bytes", 0),
        ("source_docx_max_entries", 0),
    ],
)
def test_bad_numbers_are_a_startup_error_that_names_the_setting(name, value):
    with pytest.raises((ConfigError, ValidationError, ValueError)) as caught:
        make_settings(**{name: value})
    assert name.upper() in str(caught.value)


def test_the_quota_must_hold_at_least_one_maximum_upload():
    with pytest.raises((ConfigError, ValidationError, ValueError), match="SOURCE_ROOM_QUOTA_BYTES"):
        make_settings(source_max_upload_bytes=10_000, source_room_quota_bytes=5_000)


@pytest.mark.parametrize("roles", [[], ["admin"], ["editor", "viewer"], ["owner", "everyone"]])
def test_download_roles_must_be_known_roles_and_include_the_owner(roles):
    with pytest.raises((ConfigError, ValidationError, ValueError), match="SOURCE_DOWNLOAD_ROLES"):
        make_settings(source_download_roles=roles)


def test_sources_need_no_ai_configuration():
    s = make_settings(ai_enabled=False)
    assert isinstance(s, Settings) and s.ai_enabled is False


def test_the_cleanup_loader_needs_only_the_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("STORAGE_PATH", raising=False)
    loaded = load_ai_cleanup_settings()
    assert loaded.storage_path


# ---- migration 0008 -----------------------------------------------------------------------------------


async def sql(statement: str, **params):
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return result.all() if result.returns_rows else None
    finally:
        await engine.dispose()


@pytest.fixture
def at_head():
    build_fresh_schema()
    yield
    build_fresh_schema()


def test_upgrade_adds_exactly_the_three_source_tables(at_head):
    assert set(SOURCE_TABLES) <= table_names()
    assert set(SOURCE_TABLES) == {"sources", "source_versions", "source_uploads"}


def test_downgrade_removes_only_the_source_tables_and_upgrade_works_again(at_head):
    asyncio.run(sql("INSERT INTO users (id, display_name) VALUES (gen_random_uuid(), 'u')"))
    asyncio.run(
        sql("INSERT INTO rooms (name, owner_id, event_seq) SELECT 'r', id, 1 FROM users LIMIT 1")
    )
    asyncio.run(
        sql(
            "INSERT INTO sources (room_id, created_by, display_name) "
            "SELECT id, owner_id, 'x.pdf' FROM rooms"
        )
    )
    config = alembic_config()
    command.downgrade(config, "0007")
    names = table_names()
    assert not (set(SOURCE_TABLES) & names)
    assert {"rooms", "users", "ai_usage_ledger", "ai_global_flags"} <= names  # nothing else touched
    assert asyncio.run(sql("SELECT count(*) FROM rooms")) == [(1,)]
    command.upgrade(config, "head")
    assert set(SOURCE_TABLES) <= table_names()
    assert asyncio.run(sql("SELECT count(*) FROM sources")) == [(0,)]


def test_the_models_and_the_migration_agree(at_head):
    command.check(alembic_config())  # "No new upgrade operations detected"
