"""The bootstrap snapshot behind GET /rooms/{room_id}/sync.

WHY THIS IS CONSISTENT. Every supported mutation does three things in ONE transaction while
holding the room lock: change the data, bump rooms.event_seq, insert the room_events row. A
REPEATABLE READ transaction sees every statement against the same committed state, fixed when its
FIRST query runs. So it sees each such transaction entirely or not at all: the members, messages
and room row it reads match `event_seq` exactly, and `through_seq` means "the data below includes
the effect of every event up to here, and no event after".

WHY A SEPARATE SESSION. The isolation level cannot be changed after a transaction's first query
(PostgreSQL: SET TRANSACTION ISOLATION LEVEL must precede the first query). The authentication
dependency already ran queries on the request session, so this uses a NEW session whose isolation
level and READ ONLY flag are set (execution options) before its first statement. Nothing in here
talks to Redis or a socket; the transaction lasts for three short reads.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import room_not_found
from app.models import Membership, Room
from app.schemas.messages import MessageOut
from app.schemas.rooms import MemberOut
from app.schemas.sync import SyncOut, SyncRoom
from app.services import memberships, messages

SYNC_MESSAGE_LIMIT = 50
_ROOMS = Room.__table__
_MEMBERSHIPS = Membership.__table__


@asynccontextmanager
async def open_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A new READ ONLY REPEATABLE READ transaction. The options are applied to the connection
    BEFORE the transaction's first statement; the snapshot itself is taken by the first query."""
    async with sessionmaker() as db:
        await db.connection(
            execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
        )
        try:
            yield db
        finally:
            await db.rollback()  # read-only: nothing to commit; just end the transaction


async def build_sync(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    room_id: uuid.UUID,
    user_id: uuid.UUID,
) -> SyncOut:
    async with open_snapshot(sessionmaker) as db:
        # FIRST statement: the membership recheck AND the room row, so authorization and data are
        # judged in the very same snapshot (a removal committed after this point is not visible
        # here, which is correct: the response then describes the world as of the snapshot).
        row = (
            await db.execute(
                select(_ROOMS, _MEMBERSHIPS.c.role.label("my_role"))
                .join(_MEMBERSHIPS, _MEMBERSHIPS.c.room_id == _ROOMS.c.id)
                .where(
                    _ROOMS.c.id == room_id,
                    _MEMBERSHIPS.c.user_id == user_id,
                    _MEMBERSHIPS.c.state == "active",
                )
            )
        ).one_or_none()
        if row is None:
            raise room_not_found()
        members = await memberships.fetch_active_members(db, room_id, settings.max_members_per_room)
        page, next_before = await messages.fetch_message_page(db, room_id, SYNC_MESSAGE_LIMIT, None)
    return SyncOut(
        room=SyncRoom(
            id=row.id,
            name=row.name,
            owner_id=row.owner_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            active_document_id=row.active_document_id,
            active_document_version=row.active_document_version,
        ),
        role=row.my_role,
        membership_epoch=row.membership_epoch,
        members=[MemberOut(**m.__dict__) for m in members],
        messages=[MessageOut(**m.__dict__) for m in page],
        next_before=next_before,
        through_seq=row.event_seq,
    )
