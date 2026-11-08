"""Async database engine and session helpers.

Concurrency rule: one AsyncSession per request or worker operation, never shared across
concurrent tasks. An AsyncSession only checks out a connection when it first runs a statement and
returns it on commit/rollback/close, so short `async with sessionmaker()` blocks around each unit
of work keep connections (and transactions) away from slow network calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine(settings: Settings, **engine_kwargs: Any) -> AsyncEngine:
    """Create the engine. No connection is opened until first use."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        # Keep bound parameters (nonce, PKCE verifier, hashes) out of SQLAlchemy error messages.
        hide_parameters=True,
        # Store and compare everything in UTC regardless of the server's default timezone.
        connect_args={"server_settings": {"timezone": "UTC"}, "timeout": 5},
        **engine_kwargs,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False so objects stay readable after commit without lazy-loading
    # (lazy loads are not possible on an AsyncSession).
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Handlers commit explicitly; anything uncommitted is rolled back."""
    async with request.app.state.sessionmaker() as session:
        yield session
