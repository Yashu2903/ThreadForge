"""Cross-cutting platform behaviour: proxy trust, test-harness safety, CORS, production docs."""

import asyncio
import logging
import os
import subprocess
import sys

import httpx2

from app.main import create_app
from tests.db_utils import BACKEND_DIR
from tests.factories import make_production_settings, make_settings


async def access_log_client(settings, engine, caplog, *, peer, headers):
    caplog.set_level(logging.INFO, logger="threadforge")
    app = create_app(settings, engine=engine)
    transport = httpx2.ASGITransport(app=app, client=(peer, 40000), raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        await c.get("/health/live", headers=headers)
    line = next(r.getMessage() for r in caplog.records if r.name == "threadforge.access")
    return line.rsplit("client=", 1)[1]


async def test_forwarded_for_is_honoured_only_from_a_trusted_proxy(engine, caplog):
    spoofed = {"X-Forwarded-For": "203.0.113.9"}

    trusted = make_settings(trusted_proxy_ips="10.0.0.5")
    assert await access_log_client(trusted, engine, caplog, peer="10.0.0.5", headers=spoofed) == (
        "203.0.113.9"
    )

    caplog.clear()
    # Same header from an address that is not a configured proxy: ignored (cannot be spoofed).
    assert await access_log_client(
        trusted, engine, caplog, peer="198.51.100.7", headers=spoofed
    ) == ("198.51.100.7")

    caplog.clear()
    # Default configuration trusts nobody.
    default = make_settings()
    assert await access_log_client(default, engine, caplog, peer="10.0.0.5", headers=spoofed) == (
        "10.0.0.5"
    )


def test_harness_refuses_a_database_not_named_with_test_suffix():
    env = {
        **os.environ,
        "TEST_DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/threadforge",  # not *_test
    }
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_health.py"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode != 0  # pytest reports an exit during conftest import as 4
    assert "must end with '_test'" in completed.stdout + completed.stderr


async def test_no_cors_headers_are_emitted(client):
    response = await client.get("/health/live", headers={"Origin": "https://evil.example"})
    assert not any(h.lower().startswith("access-control-") for h in response.headers)
    preflight = await client.options(
        "/auth/logout",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 405
    assert not any(h.lower().startswith("access-control-") for h in preflight.headers)


async def test_api_docs_are_disabled_in_production(engine):
    app = create_app(make_production_settings(), engine=engine)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(
        transport=transport, base_url="https://api.threadforge.test"
    ) as c:
        for path in ("/docs", "/openapi.json", "/redoc"):
            assert (await c.get(path)).status_code == 404


async def test_app_startup_does_not_create_schema_or_touch_the_database():
    """create_app must not connect: it works with a database that does not exist, and the
    schema comes only from Alembic (no create_all)."""
    settings = make_settings(database_url="postgresql+asyncpg://nobody:x@127.0.0.1:1/never_test")
    app = create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        assert (await c.get("/health/live")).status_code == 200
    await asyncio.wait_for(app.state.engine.dispose(), timeout=5)
