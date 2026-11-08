"""FastAPI application factory.

Run with:  uvicorn app.main:create_app --factory --no-access-log --no-proxy-headers
(see README for why those two flags matter).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import health
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.errors import install_error_handlers
from app.logging_config import configure_logging
from app.middleware import RequestContextMiddleware


def create_app(settings: Settings | None = None, engine: AsyncEngine | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # No schema creation here. Schema changes happen only through Alembic.
        # No network calls here either: Google metadata is fetched lazily on first login.
        yield
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

    install_error_handlers(app)
    app.include_router(health.router)

    # add_middleware puts the newest outermost: proxy headers are resolved first so that the
    # request-context middleware (and its logs) see the real client address.
    app.add_middleware(RequestContextMiddleware)
    if settings.trusted_proxy_ips:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_ips)
    return app
