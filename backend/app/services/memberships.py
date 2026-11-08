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

from sqlalchemy import func, insert, select, tuple_, update
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


async def activate_membership(
    db: AsyncSession, room_id: uuid.UUID, user_id: uuid.UUID, role: str, existing
):
    """Create the membership, or REACTIVATE the retained removed row (same row, reset joined_at,
    cleared removed_at). Call under the room lock. The caller bumps membership_epoch."""
    if existing is None:
        statement = (
            insert(_MEMBERSHIPS)
            .values(room_id=room_id, user_id=user_id, role=role, state="active")
            .returning(*_MEMBERSHIPS.c)
        )
    else:
        statement = (
            update(_MEMBERSHIPS)
            .where(_MEMBERSHIPS.c.room_id == room_id, _MEMBERSHIPS.c.user_id == user_id)
            .values(
                role=role,
                state="active",
                joined_at=func.now(),
                removed_at=None,
                updated_at=func.now(),
            )
            .returning(*_MEMBERSHIPS.c)
        )
    return (await db.execute(statement)).one()


async def lock_membership(db: AsyncSession, room_id: uuid.UUID, user_id: uuid.UUID):
    """Lock-order step 3: the membership row, FOR UPDATE (so a Part 3 FOR SHARE check conflicts
    with, and is ordered against, every change to it). None if the row does not exist."""
    return (
        await db.execute(
            select(_MEMBERSHIPS)
            .where(_MEMBERSHIPS.c.room_id == room_id, _MEMBERSHIPS.c.user_id == user_id)
            .with_for_update()
        )
    ).one_or_none()


async def member_view(db: AsyncSession, membership) -> MemberView:
    name = (
        await db.execute(select(_USERS.c.display_name).where(_USERS.c.id == membership.user_id))
    ).scalar_one()
    return MemberView(membership.user_id, name, membership.role, membership.joined_at)
