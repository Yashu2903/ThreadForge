"""TEST-ONLY: create and migrate the dedicated browser-test database, and empty it.

    uv run python -m scripts.e2e_prepare

The Playwright suite runs the real API against ONE database named `threadforge_e2e`, never the
development database and never the pytest database. This script creates it if needed, applies the
Alembic migrations, and truncates every application table so each run starts empty. It refuses any
other database name.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
E2E_DB = "threadforge_e2e"
# The collaboration service tests run the real API against their own database too.
ALLOWED_DBS = (E2E_DB, "threadforge_collab_e2e")
DEFAULT_URL = f"postgresql+asyncpg://threadforge:threadforge-dev-password@127.0.0.1:5442/{E2E_DB}"


async def ensure_database(url: str) -> None:
    parsed = make_url(url)
    if parsed.database not in ALLOWED_DBS:
        raise SystemExit(f"refusing to touch database {parsed.database!r}: only {ALLOWED_DBS!r}")
    assert re.fullmatch(r"[a-z0-9_]+", parsed.database)
    connection = await asyncpg.connect(
        user=parsed.username,
        password=parsed.password,
        host=parsed.host,
        port=parsed.port,
        database="postgres",
        timeout=10,
    )
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", parsed.database
        )
        if not exists:
            await connection.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await connection.close()


async def truncate(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            names = (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        " AND tablename <> 'alembic_version'"
                    )
                )
            ).scalars()
            tables = ", ".join(f'"{name}"' for name in names)
            if tables:
                await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))  # noqa: S608
    finally:
        await engine.dispose()


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    asyncio.run(ensure_database(url))
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = url
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")
    asyncio.run(truncate(url))
    sys.stdout.write(f"{make_url(url).database} ready\n")


if __name__ == "__main__":
    main()
