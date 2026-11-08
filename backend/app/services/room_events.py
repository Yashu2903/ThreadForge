"""Durable room events: the recording helper and the history read.

record_room_event() is the ONLY place a room_events row, an outbox row, or rooms.event_seq is
written. It runs INSIDE the caller's transaction (the business change, its event and its outbox
row commit together or not at all) and never touches Redis.

Sequence allocation, one statement under the room lock:

    UPDATE rooms SET event_seq = event_seq + 1, updated_at = updated_at WHERE id = :room
    RETURNING event_seq

Because the room lock is held until commit, concurrent writers in a room queue up, so sequences are
contiguous and are committed in sequence order: a later-committing transaction never has a lower
seq. A PostgreSQL SEQUENCE would NOT give that (sequence values are consumed outside transactions,
so rolled-back or reordered transactions leave gaps and out-of-order commits), and MAX(seq) + 1
would race. Setting updated_at to itself stops the column's ORM onupdate default from firing:
allocating a sequence number must not change rooms.version or rooms.updated_at.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AppError
from app.models import OutboxEntry, Room, RoomEvent
from app.schemas.events import (
    EVENT_SCHEMA_VERSION,
    OutboxEnvelope,
    RoomEventOut,
    validate_event_fields,
)
from app.services.room_lock import LockedRoom

_ROOMS = Room.__table__
_EVENTS = RoomEvent.__table__
_OUTBOX = OutboxEntry.__table__


@dataclass(frozen=True)
class RecordedEvent:
    event_id: uuid.UUID
    room_seq: int


async def record_room_event(
    db: AsyncSession,
    locked_room: LockedRoom,
    *,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID,
    resource_version: int | None,
    actor_id: uuid.UUID,
    payload: dict[str, Any],
) -> RecordedEvent:
    """Allocate the next room sequence, insert the event, insert its pending outbox row.

    Call after the change has been applied and before the single commit. Only a LockedRoom (which
    only the room-locking functions return) is accepted, so this cannot run without the lock.
    """
    if not isinstance(locked_room, LockedRoom):
        raise TypeError("record_room_event requires a LockedRoom from lock_room()/adopt_new_room()")
    room_id = locked_room.id
    clean_payload = validate_event_fields(
        event_type, resource_type, resource_id, resource_version, payload
    )

    # 1. the sequence allocation (one statement, under the room lock)
    seq = (
        await db.execute(
            update(_ROOMS)
            .where(_ROOMS.c.id == room_id)
            .values(event_seq=_ROOMS.c.event_seq + 1, updated_at=_ROOMS.c.updated_at)
            .returning(_ROOMS.c.event_seq)
        )
    ).scalar_one()

    # 2. the durable event (created_at from clock_timestamp(), read after the lock is held)
    event_id = (
        await db.execute(
            insert(_EVENTS)
            .values(
                room_id=room_id,
                seq=seq,
                schema_version=EVENT_SCHEMA_VERSION,
                type=event_type,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_version=resource_version,
                actor_id=actor_id,
                payload=clean_payload,
                created_at=func.clock_timestamp(),
            )
            .returning(_EVENTS.c.event_id)
        )
    ).scalar_one()

    # 3. the publication work, ready now. The envelope is ids only (re-validated by the
    # dispatcher before publishing).
    envelope = OutboxEnvelope(
        schema_version=1, event_id=event_id, room_id=room_id, room_seq=seq
    ).model_dump(mode="json")
    await db.execute(
        insert(_OUTBOX).values(
            event_id=event_id,
            room_id=room_id,
            room_seq=seq,
            kind="room_event",
            payload=envelope,
            state="pending",
            next_attempt_at=func.now(),
        )
    )
    return RecordedEvent(event_id=event_id, room_seq=seq)


# ---- reads ---------------------------------------------------------------------------------------


def row_to_event(row: Any) -> RoomEventOut:
    return RoomEventOut(
        schema_version=row.schema_version,
        event_id=row.event_id,
        room_id=row.room_id,
        room_seq=row.seq,
        event_type=row.type,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        resource_version=row.resource_version,
        actor_id=row.actor_id,
        payload=row.payload,
        created_at=row.created_at,
    )


def cursor_ahead(current_room_seq: int) -> AppError:
    return AppError(
        409,
        "cursor_ahead",
        "after_seq is ahead of this room's latest event.",
        extra={"current_room_seq": current_room_seq},
    )


async def list_events(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    after_seq: int,
    limit: int,
) -> tuple[list[RoomEventOut], int, bool, int]:
    """Durable history after `after_seq`, ascending. Returns (items, next_after_seq, has_more,
    current_room_seq).

    CONSISTENCY: items and current_room_seq come from ONE statement (rooms LEFT JOIN room_events),
    and a single statement sees one snapshot even at READ COMMITTED, so the two always agree:
    current_room_seq is never behind or ahead of the events returned in the same response.
    limit + 1 rows are fetched to compute has_more without a count query.
    """
    query = (
        select(
            _ROOMS.c.event_seq.label("current_room_seq"),
            *[c for c in _EVENTS.c],
        )
        .select_from(
            _ROOMS.outerjoin(
                _EVENTS, (_EVENTS.c.room_id == _ROOMS.c.id) & (_EVENTS.c.seq > after_seq)
            )
        )
        .where(_ROOMS.c.id == room_id)
        .order_by(_EVENTS.c.seq.asc())
        .limit(limit + 1)
    )
    async with sessionmaker() as db:
        rows = (await db.execute(query)).all()
    current = rows[0].current_room_seq if rows else 0
    if after_seq > current:
        raise cursor_ahead(current)
    events = [row for row in rows if row.seq is not None]
    page, extra = events[:limit], events[limit:]
    items = [row_to_event(row) for row in page]
    return items, (items[-1].room_seq if items else after_seq), bool(extra), current
