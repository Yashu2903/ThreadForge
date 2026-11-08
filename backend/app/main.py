"""FastAPI application factory.

Run with:  uvicorn app.main:create_app --factory --no-access-log --no-proxy-headers
(see README for why those two flags matter).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import (
    auth,
    health,
    invitations,
    members,
    messages,
    realtime,
    room_events,
    rooms,
)
from app.body_limit import BodySizeLimitMiddleware
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.errors import install_error_handlers
from app.logging_config import configure_logging
from app.middleware import RequestContextMiddleware
from app.realtime.hub import RealtimeHub
from app.services.oidc import OIDCClient


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    oidc_transport: httpx2.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # No schema creation here. Schema changes happen only through Alembic.
        # No network calls needed to start: Google metadata is fetched lazily on first login, and
        # the realtime hub connects to Redis in the background (the API serves HTTP, and
        # /health/ready stays database-only, whether or not Redis is reachable).
        await app.state.realtime.start()
        yield
        await app.state.realtime.stop()  # closes every WebSocket with 1001, then Redis
        await app.state.engine.dispose()

    app = FastAPI(
        title="ThreadForge API",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    app.state.engine = engine or create_engine(settings)
    app.state.sessionmaker = create_sessionmaker(app.state.engine)
    # No network I/O at construction: discovery and JWKS are fetched lazily on first login.
    # oidc_transport exists so tests can stub Google at the HTTP level.
    app.state.oidc = OIDCClient(settings, transport=oidc_transport)
    app.state.realtime = RealtimeHub(settings, app.state.sessionmaker)

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(rooms.router)
    app.include_router(invitations.router)
    app.include_router(members.router)
    app.include_router(messages.router)
    app.include_router(room_events.router)
    app.include_router(realtime.router)

    # add_middleware puts the newest outermost: proxy headers are resolved first so that the
    # request-context middleware (and its logs) see the real client address.
    # Innermost of the three: the 413 it produces still gets a request id and no-store.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    if settings.trusted_proxy_ips:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_ips)
    return app
