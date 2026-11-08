"""Shared fixtures.

Safety: the suite refuses to run unless the database name ends in `_test`, and it never touches
any other database. The database is created on demand if it does not exist.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

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
from app.services.sessions import create_session
from tests.db_utils import APP_TABLES, build_fresh_schema
from tests.factories import (
    TEST_CLIENT_ID,
    TEST_CLIENT_SECRET,
    TEST_DATABASE_URL,
    TEST_REDIS_DB_INDEX,
    TEST_REDIS_URL,
    make_settings,
)
from tests.fake_google import FakeGoogle
from tests.helpers import Actor, make_client, put_cookie
from tests.realtime_helpers import Pump, live_server


def require_test_database(url: str) -> None:
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        pytest.exit(
            f"Refusing to run: test database name {name!r} must end with '_test'.", returncode=2
        )


require_test_database(TEST_DATABASE_URL)


def require_test_redis(url: str) -> None:
    """Redis is used only for pub/sub, under a unique prefix, but still refuse to touch any
    database index other than the dedicated test one (15)."""
    index = urlsplit(url).path.strip("/")
    if index != TEST_REDIS_DB_INDEX:
        pytest.exit(
            f"Refusing to run: test Redis URL must use the dedicated database index "
            f"{TEST_REDIS_DB_INDEX}, got {index!r}.",
            returncode=2,
        )


require_test_redis(TEST_REDIS_URL)
RUN_ID = uuid.uuid4().hex[:8]


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
def redis_prefix() -> str:
    """A unique channel prefix per test, so tests can never see each other's notifications."""
    prefix = f"tftest-{RUN_ID}-{uuid.uuid4().hex[:8]}"
    assert prefix.startswith("tftest-")
    return prefix


@pytest.fixture
def settings(redis_prefix: str) -> Settings:
    return make_settings(redis_channel_prefix=redis_prefix)


@pytest_asyncio.fixture
async def redis_client():
    """A raw Redis client for tests (subscribing, publishing, checking subscriber counts)."""
    import redis.asyncio as aioredis

    require_test_redis(TEST_REDIS_URL)
    client = aioredis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # infrastructure missing: fail loudly, never skip silently
        await client.aclose()
        pytest.fail(
            f"Redis is not reachable at the test URL ({type(exc).__name__}). "
            "Start it with: docker compose up -d --wait redis"
        )
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    require_test_database(settings.database_url.get_secret_value())
    # NullPool: no connection outlives the test's event loop (asyncpg connections are bound to
    # the loop that created them, and each test gets its own loop).
    # 3 s lock timeout: a deadlock or lock-ordering bug fails a test fast instead of hanging it.
    engine = create_engine(settings, poolclass=NullPool, lock_timeout_ms=3000)
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


@pytest_asyncio.fixture
async def make_actor(settings, engine, sessionmaker, google):
    """Factory for signed-in users. The user row is inserted directly; the SESSION is created by
    Part 1's real create_session(), and the cookie is set on a fresh client, so every request
    exercises the real cookie -> hash -> lookup path. Nothing here exists in production code."""
    async with AsyncExitStack() as stack:

        async def factory(name: str = "User", *, app_settings: Settings | None = None) -> Actor:
            app_settings = app_settings or settings
            async with sessionmaker() as db, db.begin():
                user_id = (
                    await db.execute(
                        text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"),
                        {"n": name},
                    )
                ).scalar_one()
                session = await create_session(db, app_settings, user_id)
            client = await stack.enter_async_context(make_client(app_settings, engine, google))
            put_cookie(client, app_settings.session_cookie_name, session.secret)
            actor = Actor(client, user_id, name)
            actor.secret = session.secret
            actor.session_id = session.id
            return actor

        yield factory


@pytest_asyncio.fixture
async def alice(make_actor) -> Actor:
    return await make_actor("Alice")


@pytest_asyncio.fixture
async def bob(make_actor) -> Actor:
    return await make_actor("Bob")


@pytest_asyncio.fixture
async def carol(make_actor) -> Actor:
    return await make_actor("Carol")


@pytest_asyncio.fixture
async def live(settings, engine, redis_client):
    """The API under a real uvicorn server (real lifespan: the hub starts and stops for real)."""
    async with live_server(settings, engine) as server:
        yield server


@pytest_asyncio.fixture
async def pump(settings, sessionmaker, redis_client):
    """An in-process outbox dispatcher publishing to the real Redis."""
    pump = Pump(sessionmaker, settings)
    yield pump
    await pump.aclose()
