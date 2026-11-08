import asyncio
import logging

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.errors import AppError
from app.logging_config import APP_LOGGER

router = APIRouter(prefix="/health", tags=["health"])
log = logging.getLogger(f"{APP_LOGGER}.health")


@router.get("/live")
async def live() -> dict[str, str]:
    """Process is up. Deliberately touches nothing else, so an orchestrator never restarts the
    app just because PostgreSQL is briefly unavailable."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, str]:
    """Ready to serve: PostgreSQL answers a trivial query within a bounded time.
    Never calls Google, so it passes with no internet access."""
    settings = request.app.state.settings
    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            async with request.app.state.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as exc:  # any failure at all means "not ready"
        log.warning("readiness check failed: %s", type(exc).__name__)
        raise AppError(503, "database_unavailable", "The database is not available.") from None
    return {"status": "ok"}
