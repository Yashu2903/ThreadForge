"""Helpers for building and inspecting the test database schema."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.factories import TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parents[1]
PART1_TABLES = ("users", "auth_identities", "sessions", "oidc_login_attempts")
PART2_TABLES = ("rooms", "memberships", "invitations")
PART3_TABLES = ("messages", "message_write_buckets")
PART4_TABLES = ("room_events", "outbox")
PART6_TABLES = ("documents", "room_assets", "document_asset_refs", "document_write_buckets")
APP_TABLES = (*PART1_TABLES, *PART2_TABLES, *PART3_TABLES, *PART4_TABLES, *PART6_TABLES)


def alembic_config(url: str = TEST_DATABASE_URL) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = url
    config.attributes["configure_logging"] = False  # keep pytest's log capture intact
    return config


async def _run(statement: str | None = None, *, tables: bool = False):
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            if statement:
                await connection.execute(text(statement))
            if tables:
                return await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await engine.dispose()
    return None


def reset_schema() -> None:
    """Drop everything in the test database (only ever the *_test database)."""
    asyncio.run(_run("DROP SCHEMA public CASCADE"))
    asyncio.run(_run("CREATE SCHEMA public"))


def table_names() -> set[str]:
    return asyncio.run(_run(tables=True))


def upgrade_to_head() -> None:
    command.upgrade(alembic_config(), "head")


def build_fresh_schema() -> None:
    reset_schema()
    upgrade_to_head()
