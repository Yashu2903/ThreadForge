"""Membership reads and writes.

All writes to the `memberships` table live in this module (and `invitations.activate_membership`
callers go through it); routes never write membership rows. Part 4 will add room-event and
outbox writes inside these same transactions, so these functions are the single place that edit
happens. Locks: see the lock order documented in app/services/rooms.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Membership, User
from app.pagination import decode_cursor, encode_cursor

_MEMBERSHIPS = Membership.__table__
_USERS = User.__table__


@dataclass(frozen=True)
class MemberView:
    user_id: uuid.UUID
    display_name: str
    role: str
    joined_at: datetime


async def get_active_role(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, user_id: uuid.UUID
) -> str | None:
    """Fast pre-check used by dependencies: the caller's role if ACTIVE, else None.
    Never the authority for a mutation (services re-check under the room lock)."""
    async with sessionmaker() as db:
        return (
            await db.execute(
                select(_MEMBERSHIPS.c.role).where(
                    _MEMBERSHIPS.c.room_id == room_id,
                    _MEMBERSHIPS.c.user_id == user_id,
                    _MEMBERSHIPS.c.state == "active",
                )
            )
        ).scalar_one_or_none()


async def list_members(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[MemberView], str | None]:
    """Active members only; no email or provider data. Oldest joiner first."""
    after = decode_cursor(cursor)
    query = (
        select(
            _MEMBERSHIPS.c.user_id,
            _USERS.c.display_name,
            _MEMBERSHIPS.c.role,
            _MEMBERSHIPS.c.joined_at,
        )
        .join(_USERS, _USERS.c.id == _MEMBERSHIPS.c.user_id)
        .where(_MEMBERSHIPS.c.room_id == room_id, _MEMBERSHIPS.c.state == "active")
        .order_by(_MEMBERSHIPS.c.joined_at.asc(), _MEMBERSHIPS.c.user_id.asc())
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(
            tuple_(_MEMBERSHIPS.c.joined_at, _MEMBERSHIPS.c.user_id) > tuple_(*after)
        )
    async with sessionmaker() as db:
        rows = (await db.execute(query)).all()
    page, extra = rows[:limit], rows[limit:]
    items = [MemberView(r.user_id, r.display_name, r.role, r.joined_at) for r in page]
    next_cursor = encode_cursor(page[-1].joined_at, page[-1].user_id) if extra else None
    return items, next_cursor
