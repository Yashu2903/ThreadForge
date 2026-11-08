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


# ---- Part 2: actors (a user + a real session + their own browser) ------------------------------

TRUSTED_ORIGIN = "http://localhost:8000"


class Actor:
    """A signed-in user with their own client (cookie jar). Mutating calls automatically carry a
    CSRF token fetched from the real /auth/csrf endpoint and a trusted Origin; pass csrf=False /
    origin=False to omit them."""

    def __init__(self, client: httpx2.AsyncClient, user_id, name: str) -> None:
        self.client = client
        self.id = user_id
        self.name = name
        self.secret: str | None = None  # the raw session cookie value (tests only), for WS clients
        self._csrf: str | None = None

    async def csrf_token(self) -> str:
        if self._csrf is None:
            response = await self.client.get("/auth/csrf")
            assert response.status_code == 200, response.text
            self._csrf = response.json()["csrf_token"]
        return self._csrf

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        csrf: bool = True,
        origin: bool = True,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx2.Response:
        sent = dict(headers or {})
        if method.upper() not in ("GET", "HEAD"):
            if csrf:
                sent["X-CSRF-Token"] = await self.csrf_token()
            if origin:
                sent["Origin"] = TRUSTED_ORIGIN
        if content is not None:
            return await self.client.request(method, url, content=content, headers=sent, **kwargs)
        return await self.client.request(method, url, json=json, headers=sent, **kwargs)

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self.request("POST", url, **kw)

    async def patch(self, url, **kw):
        return await self.request("PATCH", url, **kw)

    async def put(self, url, **kw):
        return await self.request("PUT", url, **kw)

    async def delete(self, url, **kw):
        return await self.request("DELETE", url, **kw)


async def add_member(sm, room_id, user_id, role="editor", state="active") -> None:
    """Insert/overwrite a membership directly (setup only; the API paths are tested separately)."""
    removed = "now()" if state == "removed" else "NULL"  # fixed literals, not user input
    await execute(
        sm,
        f"INSERT INTO memberships (room_id, user_id, role, state, removed_at) "
        f"VALUES (:r, :u, :role, :s, {removed}) "
        f"ON CONFLICT (room_id, user_id) DO UPDATE SET role = :role, state = :s, "
        f"removed_at = {removed}",
        r=room_id,
        u=user_id,
        role=role,
        s=state,
    )


async def room_row(sm, room_id) -> dict[str, Any]:
    [row] = await rows(sm, "SELECT * FROM rooms WHERE id = :r", r=room_id)
    return row


async def insert_messages(
    sm,
    room_id,
    author_id,
    count: int,
    *,
    start=None,
    step=None,
    prefix: str = "msg",
):
    """Insert `count` messages directly with CHOSEN timestamps (start + i*step, i=1..count), so
    time-dependent behaviour is tested without sleeping. step=timedelta(0) gives identical
    timestamps. Returns the ids in insertion (i) order."""
    from datetime import UTC, datetime, timedelta

    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    step = timedelta(seconds=1) if step is None else step
    async with sm() as db, db.begin():  # its own transaction: rows() would not commit
        result = (
            await db.execute(
                text(
                    "WITH ins AS ("
                    " INSERT INTO messages (room_id, author_id, client_message_id, body,"
                    "  initial_request_fingerprint, created_at)"
                    " SELECT :r, :a, gen_random_uuid(), CAST(:p AS text) || ' ' || g::text,"
                    "  decode(repeat('ab', 32), 'hex'),"
                    "  CAST(:s AS timestamptz) + g * CAST(:st AS interval)"
                    " FROM generate_series(1, CAST(:n AS integer)) AS g RETURNING id, body)"
                    " SELECT id FROM ins ORDER BY length(body), body"
                ),
                {"r": room_id, "a": author_id, "p": prefix, "s": start, "st": step, "n": count},
            )
        ).all()
    return [row.id for row in result]


async def seed_outbox(sm, owner_id, n: int, *, room_id=None):
    """Insert a room (unless given) plus n events and n PENDING outbox rows, directly. Returns
    (room_id, [event_id, ...] in seq order). For dispatcher tests that need many rows quickly."""
    if room_id is None:
        await execute(sm, "INSERT INTO rooms (name, owner_id) VALUES ('seeded', :o)", o=owner_id)
        room_id = await scalar(
            sm, "SELECT id FROM rooms WHERE name = 'seeded' ORDER BY created_at DESC LIMIT 1"
        )
    first = await scalar(
        sm, "SELECT coalesce(max(seq), 0) + 1 FROM room_events WHERE room_id = :r", r=room_id
    )
    async with sm() as db, db.begin():
        result = await db.execute(
            text(
                "WITH ev AS ("
                " INSERT INTO room_events (room_id, seq, type, resource_type, resource_id, actor_id,"
                "  payload, created_at)"
                " SELECT :r, g, 'room_renamed', 'room', :r, :o, '{}', clock_timestamp()"
                " FROM generate_series(CAST(:first AS integer), CAST(:first AS integer) + CAST(:n AS integer) - 1) g"
                " RETURNING room_id, seq, event_id), ob AS ("
                " INSERT INTO outbox (event_id, room_id, room_seq, payload)"
                " SELECT event_id, room_id, seq, jsonb_build_object('schema_version', 1,"
                "  'event_id', event_id::text, 'room_id', room_id::text, 'room_seq', seq) FROM ev"
                " RETURNING event_id, room_seq) SELECT event_id FROM ob ORDER BY room_seq"
            ),
            {"r": room_id, "o": owner_id, "first": first, "n": n},
        )
        ids = [row.event_id for row in result.all()]
    return room_id, ids


async def append_events(sm, room_id, n: int, *, actor_id=None, event_type: str = "room_renamed"):
    """Append `n` valid events to a room in ONE transaction exactly like a service function does:
    take the room lock, bump rooms.event_seq by n, insert the room_events rows. Does not touch
    the outbox (no hint is ever published). Returns the new sequence numbers."""
    async with sm() as db, db.begin():
        row = (
            await db.execute(
                text("SELECT event_seq, owner_id FROM rooms WHERE id = :r FOR NO KEY UPDATE"),
                {"r": room_id},
            )
        ).one()
        first = row.event_seq + 1
        await db.execute(
            text(
                "UPDATE rooms SET event_seq = event_seq + :n, updated_at = updated_at WHERE id = :r"
            ),
            {"n": n, "r": room_id},
        )
        await db.execute(
            text(
                "INSERT INTO room_events (room_id, seq, type, resource_type, resource_id, actor_id,"
                " payload, created_at)"
                " SELECT :r, g, :t, 'room', :r, :a, '{}', clock_timestamp()"
                " FROM generate_series(CAST(:first AS integer), CAST(:last AS integer)) g"
            ),
            {
                "r": room_id,
                "t": event_type,
                "a": actor_id or row.owner_id,
                "first": first,
                "last": first + n - 1,
            },
        )
    return list(range(first, first + n))
