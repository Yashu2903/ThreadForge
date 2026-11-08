"""The room-row lock and the handle that proves it is held.

Every room-scoped mutation starts by locking the room row (SELECT ... FOR NO KEY UPDATE). That lock
is what serializes writers in a room, so Part 4 allocates event sequence numbers under it. To make
"allocate a sequence without holding the lock" impossible by construction, record_room_event()
accepts only a LockedRoom, and a LockedRoom can only be created by this module's two issuers:

  * lock_room(): runs the FOR NO KEY UPDATE select;
  * adopt_new_room(): for a room row INSERTED in the current transaction. Nobody else can see an
    uncommitted row, so the inserting transaction already holds it exclusively.

(Lock order is unchanged: room first; then invitation or message; then membership or bucket rows.)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Room

_ROOMS = Room.__table__
_LOCK_PROOF = object()


@dataclass(frozen=True)
class LockedRoom:
    """A room whose row lock is held by the current transaction. Do not construct directly."""

    row: Any  # the locked rooms row
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _LOCK_PROOF:
            raise TypeError(
                "LockedRoom can only be obtained from lock_room() or adopt_new_room(); "
                "constructing one by hand would bypass the room lock"
            )

    @property
    def id(self) -> uuid.UUID:
        return self.row.id


def _issue(row: Any) -> LockedRoom:
    return LockedRoom(row, _proof=_LOCK_PROOF)


async def lock_room(db: AsyncSession, room_id: uuid.UUID) -> LockedRoom | None:
    """SELECT ... FOR NO KEY UPDATE on the room row. None if the room does not exist.

    FOR NO KEY UPDATE (not FOR UPDATE) because inserts into tables with a foreign key to rooms
    (memberships, invitations, messages, room_events...) take a weak FOR KEY SHARE lock on the room
    row, which FOR UPDATE would block but FOR NO KEY UPDATE does not; mutators still exclude each
    other. Bumping rooms.event_seq changes a non-key column, so it takes the same
    FOR NO KEY UPDATE-level lock and never blocks those foreign-key inserts either.
    """
    row = (
        await db.execute(
            select(_ROOMS).where(_ROOMS.c.id == room_id).with_for_update(key_share=True)
        )
    ).one_or_none()
    return _issue(row) if row is not None else None


def adopt_new_room(row: Any) -> LockedRoom:
    """Handle for a room row INSERTED by this transaction (exclusively held until commit)."""
    return _issue(row)
