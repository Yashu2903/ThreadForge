"""Database reads for the WebSocket layer. READ-ONLY: forwarding an event never writes.

Every function opens its own short session and closes it before returning, so a connection never
holds a database session while it waits for the next frame (the request-scoped, yield-based
get_db dependency stays open until the response ends, which for a WebSocket would be the lifetime
of the socket; it is deliberately not used here).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.events import RoomEventOut
from app.services.memberships import get_active_role
from app.services.room_events import row_to_event
from app.services.sessions import AuthContext, load_session


@dataclass(frozen=True)
class HandshakeIdentity:
    """What a connection keeps: ids only. NEVER the raw cookie secret."""

    user_id: uuid.UUID
    session_id: uuid.UUID


async def authenticate_websocket(
    sessionmaker: async_sessionmaker[AsyncSession],
    secret: str | None,
    room_id: uuid.UUID | None,
) -> HandshakeIdentity | None:
    """Session valid + user active + ACTIVE member of the room, else None.

    Uses load_session(), the very function behind the HTTP get_current_user dependency, so the
    two paths cannot drift apart. The raw secret is used for this one lookup and dropped.
    """
    if not secret or room_id is None:
        return None
    async with sessionmaker() as db:
        context: AuthContext | None = await load_session(db, secret)
    if context is None or context.user.state != "active":
        return None
    if await get_active_role(sessionmaker, room_id, context.user.id) is None:
        return None
    return HandshakeIdentity(user_id=context.user.id, session_id=context.session_id)


async def read_event_seq(sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID) -> int:
    """The room's current high-water mark (rooms.event_seq), in one short read."""
    async with sessionmaker() as db:
        value = (
            await db.execute(text("SELECT event_seq FROM rooms WHERE id = :r"), {"r": room_id})
        ).scalar_one_or_none()
    return value or 0


async def read_event_seqs(
    sessionmaker: async_sessionmaker[AsyncSession], room_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """The reconciliation query: ONE statement for every room that has a local connection,
    not one per connection. rooms.event_seq is the authoritative high-water mark."""
    if not room_ids:
        return {}
    async with sessionmaker() as db:
        result = await db.execute(
            text("SELECT id, event_seq FROM rooms WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": room_ids},
        )
        return {row.id: row.event_seq for row in result.all()}


async def fetch_events_page(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    after_seq: int,
    limit: int,
) -> list[RoomEventOut]:
    """Events with seq > after_seq, ascending, at most `limit`: a primary-key range scan on
    (room_id, seq). The session is closed before this returns (never held across a socket send)."""
    async with sessionmaker() as db:
        result = await db.execute(
            text("SELECT * FROM room_events WHERE room_id = :r AND seq > :a ORDER BY seq LIMIT :l"),
            {"r": room_id, "a": after_seq, "l": limit},
        )
        return [row_to_event(row) for row in result.all()]


async def fetch_member_ids(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID
) -> set[uuid.UUID]:
    """Current ACTIVE members (presence is filtered to these)."""
    async with sessionmaker() as db:
        result = await db.execute(
            text("SELECT user_id FROM memberships WHERE room_id = :r AND state = 'active'"),
            {"r": room_id},
        )
        return {row.user_id for row in result.all()}


@dataclass(frozen=True)
class SessionAccess:
    session_ok: bool  # session not revoked, not expired, and the user is active
    member_ok: bool  # the session's user is an ACTIVE member of the room


async def check_access(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    session_ids: list[uuid.UUID],
) -> dict[uuid.UUID, SessionAccess]:
    """ONE query for all of a room's local connections: is each session still valid, and is its
    user still an active member? A session id missing from the result no longer exists.
    Database clock for expiry. Read-only; the session is closed before this returns."""
    if not session_ids:
        return {}
    async with sessionmaker() as db:
        result = await db.execute(
            text(
                "SELECT s.id AS session_id,"
                " (s.revoked_at IS NULL AND s.expires_at > now()"
                "   AND u.state = 'active') AS session_ok,"
                " EXISTS (SELECT 1 FROM memberships m WHERE m.room_id = :room"
                "   AND m.user_id = s.user_id AND m.state = 'active') AS member_ok"
                " FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"room": room_id, "ids": session_ids},
        )
        return {r.session_id: SessionAccess(r.session_ok, r.member_ok) for r in result.all()}
