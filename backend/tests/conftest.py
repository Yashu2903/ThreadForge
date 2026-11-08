"""Shared fixtures.

Safety: the suite refuses to run unless the database name ends in `_test`, and it never touches
any other database. The database is created on demand if it does not exist.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

import asyncpg
import httpx2
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db import create_engine
from app.main import create_app
from tests.db_utils import APP_TABLES, build_fresh_schema
from tests.factories import TEST_CLIENT_ID, TEST_CLIENT_SECRET, TEST_DATABASE_URL, make_settings
from tests.fake_google import FakeGoogle
from tests.helpers import make_client


def require_test_database(url: str) -> None:
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        pytest.exit(
            f"Refusing to run: test database name {name!r} must end with '_test'.", returncode=2
        )


require_test_database(TEST_DATABASE_URL)


async def _ensure_database_exists(url: str) -> None:
    parsed = make_url(url)
    name = parsed.database or ""
    assert re.fullmatch(r"[a-z0-9_]+_test", name), name
    connection = await asyncpg.connect(
        user=parsed.username,
        password=parsed.password,
        host=parsed.host,
        port=parsed.port,
        database="postgres",
        timeout=10,
    )
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> None:
    """Create the test database if needed and build its schema with Alembic (not create_all)."""
    asyncio.run(_ensure_database_exists(TEST_DATABASE_URL))
    build_fresh_schema()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    require_test_database(settings.database_url.get_secret_value())
    # NullPool: no connection outlives the test's event loop (asyncpg connections are bound to
    # the loop that created them, and each test gets its own loop).
    engine = create_engine(settings, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def google(settings: Settings) -> FakeGoogle:
    return FakeGoogle(
        client_id=TEST_CLIENT_ID,
        client_secret=TEST_CLIENT_SECRET,
        callback_url=settings.oidc_callback_url,
    )


@pytest.fixture
def app(settings: Settings, engine: AsyncEngine, google: FakeGoogle):
    return create_app(settings, engine=engine, oidc_transport=google.transport)


@pytest_asyncio.fixture
async def client(settings, engine, google) -> AsyncIterator[httpx2.AsyncClient]:
    async with make_client(settings, engine, google) as c:
        yield c
