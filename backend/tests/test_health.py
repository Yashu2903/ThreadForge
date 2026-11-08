import httpx2
import pytest_asyncio
from sqlalchemy.pool import NullPool

from app.db import create_engine
from app.main import create_app
from tests.factories import make_settings


async def test_liveness_ok(client):
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_ok_with_database(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest_asyncio.fixture
async def broken_db_client():
    """An app whose engine points at a port nothing listens on (no Docker tricks needed)."""
    settings = make_settings(
        database_url="postgresql+asyncpg://nobody:nopass@127.0.0.1:1/dead_test",
        readiness_timeout_seconds=2.0,
    )
    engine = create_engine(settings, poolclass=NullPool)
    app = create_app(settings, engine=engine)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        yield c
    await engine.dispose()


async def test_readiness_503_when_database_down(broken_db_client):
    response = await broken_db_client.get("/health/ready")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "database_unavailable"
    assert error["request_id"] == response.headers["x-request-id"]
    # No driver/host details leak to the client.
    assert "127.0.0.1" not in response.text
    assert "nobody" not in response.text


async def test_liveness_still_ok_when_database_down(broken_db_client):
    response = await broken_db_client.get("/health/live")
    assert response.status_code == 200
