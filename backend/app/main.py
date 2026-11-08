"""FastAPI application factory.

Run with:  uvicorn app.main:create_app --factory --no-access-log --no-proxy-headers
(see README for why those two flags matter).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import (
    assets,
    auth,
    capabilities,
    documents,
    health,
    internal_collab,
    invitations,
    members,
    messages,
    realtime,
    room_events,
    rooms,
    sync,
)
from app.assets_pipeline import configure_pillow
from app.body_limit import BodySizeLimitMiddleware
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.errors import install_error_handlers
from app.logging_config import configure_logging
from app.middleware import RequestContextMiddleware
from app.realtime.room_hub import RealtimeHub
from app.services.oidc import OIDCClient
from app.storage import Storage


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
    app.state.storage = Storage(settings.storage_path)
    app.state.asset_slots = asyncio.Semaphore(settings.asset_processing_concurrency)
    configure_pillow(settings.asset_max_pixels)

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(rooms.router)
    app.include_router(invitations.router)
    app.include_router(members.router)
    app.include_router(messages.router)
    app.include_router(documents.router)
    app.include_router(assets.router)
    app.include_router(capabilities.router)
    app.include_router(room_events.router)
    app.include_router(sync.router)
    app.include_router(internal_collab.router)
    app.include_router(realtime.router)

    # add_middleware puts the newest outermost: proxy headers are resolved first so that the
    # request-context middleware (and its logs) see the real client address.
    # Innermost of the three: the 413 it produces still gets a request id and no-store.
    # 128 KiB for every JSON route, except the document content save (2 MiB by default).
    app.add_middleware(
        BodySizeLimitMiddleware,
        rules=(
            (
                "PUT",
                re.compile(r"/rooms/[^/]+/documents/[^/]+/content"),
                settings.document_save_body_limit_bytes,
            ),
            # the collaboration service's full Yjs state (default 8 MiB) and validated snapshots
            (
                "PUT",
                re.compile(r"/internal/collab/documents/[^/]+/state"),
                settings.collab_max_state_bytes,
            ),
            (
                "POST",
                re.compile(r"/internal/collab/documents/[^/]+/snapshot"),
                settings.document_save_body_limit_bytes,
            ),
            # image uploads are STREAMED (never buffered) and counted as they arrive
            (
                "POST",
                re.compile(r"/rooms/[^/]+/assets"),
                settings.asset_max_upload_bytes,
                True,
            ),
        ),
    )
    app.add_middleware(RequestContextMiddleware)
    if settings.trusted_proxy_ips:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_ips)
    return app
