"""Test helpers: building clients, driving the login flow, and reading the database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from tests.fake_google import FakeGoogle


@asynccontextmanager
async def make_client(
    settings: Settings,
    engine,
    google: FakeGoogle,
    base_url: str = "http://localhost:8000",
) -> AsyncIterator[httpx2.AsyncClient]:
    """A client for a fresh app instance. Each client has its own cookie jar (its own browser)."""
    app = create_app(settings, engine=engine, oidc_transport=google.transport)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url=base_url) as client:
        yield client


def put_cookie(client: httpx2.AsyncClient, name: str, value: str) -> None:
    """Make the client send exactly this cookie value, replacing any existing one.

    The stdlib cookie jar files server-set cookies for "localhost" under the domain
    "localhost.local", so a plain cookies.set(..., domain="localhost") is silently never sent and
    a rejection test would pass for the wrong reason. Deleting by name and setting with no domain
    produces a cookie the jar really sends (test_injected_cookies_are_really_sent proves it).
    """
    client.cookies.delete(name)
    client.cookies.set(name, value)


async def start_login(client: httpx2.AsyncClient) -> httpx2.Response:
    return await client.get("/auth/login")


async def sign_in(client: httpx2.AsyncClient, google: FakeGoogle, **spec: Any) -> httpx2.Response:
    """Full browser flow: /auth/login -> (Google) -> /auth/callback. Returns the callback response."""
    login = await start_login(client)
    assert login.status_code == 303, login.text
    code, state = google.authorize(login.headers["location"], **spec)
    return await client.get("/auth/callback", params={"code": code, "state": state})


def error_code(response: httpx2.Response) -> str | None:
    """The ?error= code of a callback error redirect, or None if it is not an error redirect."""
    from urllib.parse import parse_qs, urlsplit

    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query.get("error", [None])[0]


async def rows(sessionmaker, sql: str, **params: Any) -> list[dict[str, Any]]:
    async with sessionmaker() as session:
        result = await session.execute(text(sql), params)
        return [dict(r) for r in result.mappings().all()]


async def scalar(sessionmaker, sql: str, **params: Any) -> Any:
    async with sessionmaker() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def execute(sessionmaker, sql: str, **params: Any) -> None:
    async with sessionmaker() as session:
        await session.execute(text(sql), params)
        await session.commit()
