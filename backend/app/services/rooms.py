"""Rooms: creation, reads, rename, and the locking primitives every room mutation shares.

TRANSACTION PATTERN (one pattern, used everywhere in Part 2): a service function takes the
sessionmaker, never an existing session. It opens its own session and `begin()`s one transaction,
which commits at block exit. Dependencies (auth, CSRF, fast pre-checks) use their own short
sessions that are closed before a service runs, so no transaction is ever already open.

LOCK ORDER (always, in every mutating function):
    1. the room row            SELECT ... FOR NO KEY UPDATE   (lock_room)
    2. the invitation row      SELECT ... FOR UPDATE          (redeem / revoke only)
    3. membership row(s)       SELECT ... FOR UPDATE          (role change / remove / redeem)
(Room creation additionally takes the creating user's row FOR NO KEY UPDATE first, to serialize
one user's room creations for the owned-rooms cap. No other path locks a user row, so that cannot
form a cycle.)

WHY FOR NO KEY UPDATE and not FOR UPDATE: inserting a row into a table with a foreign key to
`rooms` (a membership, an invitation) takes a weak FOR KEY SHARE lock on the room row. FOR UPDATE
conflicts with FOR KEY SHARE, so it would make those inserts wait on a mere room-level mutation;
FOR NO KEY UPDATE does not conflict with FOR KEY SHARE but does conflict with itself, so room
mutators still exclude one another. (PostgreSQL docs, "Explicit Locking", table of conflicting
row-level lock modes; the FK-insert behaviour is verified by tests/test_lock_modes.py.)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import insufficient_role, limit_reached, room_not_found
from app.logging_config import APP_LOGGER, request_id_var
from app.models import Membership, Room, User
from app.pagination import decode_cursor, encode_cursor
from app.services.room_events import record_room_event
from app.services.room_lock import adopt_new_room, lock_room

log = logging.getLogger(f"{APP_LOGGER}.rooms")

_ROOMS = Room.__table__
_MEMBERSHIPS = Membership.__table__


@dataclass(frozen=True)
class RoomView:
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    role: str  # the CALLER's role in this room
    membership_epoch: int
    created_at: datetime
    updated_at: datetime


def log_event(
    action: str, room_id: uuid.UUID, actor_id: uuid.UUID, target_id: uuid.UUID | None
) -> None:
    """Structured application log line for an access/room change. Contains no secrets."""
    log.info(
        "action=%s room_id=%s actor_id=%s target_id=%s request_id=%s",
        action,
        room_id,
        actor_id,
        target_id or "-",
        request_id_var.get(),
    )


def to_view(row, role: str) -> RoomView:
    return RoomView(
        id=row.id,
        name=row.name,
        owner_id=row.owner_id,
        role=role,
        membership_epoch=row.membership_epoch,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---- locking primitives (used by rooms, memberships and invitations services) ----------------


async def lock_room_and_actor(
    db: AsyncSession, room_id: uuid.UUID, actor_id: uuid.UUID, *, owner_only: bool
):
    """Step 1 of every room mutation: lock the room row, THEN read the actor's role.

    Reading the role only after the lock is held means no concurrent removal or demotion can slip
    in between the check and the write. Returns (LockedRoom, actor_role): the handle that
    record_room_event() requires, so events can only be recorded while the lock is held.
    Raises room_not_found for a missing room or a non-member (indistinguishable on purpose), and
    insufficient_role for a member who lacks the owner role when owner_only.
    """
    locked = await lock_room(db, room_id)
    if locked is None:
        raise room_not_found()
    role = (
        await db.execute(
            select(_MEMBERSHIPS.c.role).where(
                _MEMBERSHIPS.c.room_id == room_id,
                _MEMBERSHIPS.c.user_id == actor_id,
                _MEMBERSHIPS.c.state == "active",
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise room_not_found()
    if owner_only and role != "owner":
        raise insufficient_role()
    return locked, role


async def bump_membership_epoch(db: AsyncSession, room_id: uuid.UUID) -> int:
    """The ONLY place membership_epoch changes. Call under the room lock, only when access
    actually changed. Every real UPDATE of a rooms row also bumps version."""
    return (
        await db.execute(
            update(_ROOMS)
            .where(_ROOMS.c.id == room_id)
            .values(
                membership_epoch=_ROOMS.c.membership_epoch + 1,
                version=_ROOMS.c.version + 1,
                updated_at=func.now(),
            )
            .returning(_ROOMS.c.membership_epoch)
        )
    ).scalar_one()


# ---- operations ---------------------------------------------------------------------------------


async def create_room(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    name: str,
) -> RoomView:
    """Room + its owner membership in ONE transaction (a failure leaves no partial rows).

    The epoch starts at 1, which already represents the owner-only state, so creation does not
    bump it. The owned-rooms cap is checked under a lock on the creator's user row (FOR NO KEY
    UPDATE: does not block session or membership inserts that reference the user).
    """
    async with sessionmaker() as db, db.begin():
        await db.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True))
        owned = (
            await db.execute(
                select(func.count()).select_from(_ROOMS).where(_ROOMS.c.owner_id == user_id)
            )
        ).scalar_one()
        if owned >= settings.max_owned_rooms_per_user:
            raise limit_reached(
                "room_limit_reached", "You have reached the maximum number of rooms you can own."
            )
        room = (
            await db.execute(
                insert(_ROOMS).values(name=name, owner_id=user_id).returning(*_ROOMS.c)
            )
        ).one()
        await db.execute(
            insert(_MEMBERSHIPS).values(
                room_id=room.id, user_id=user_id, role="owner", state="active"
            )
        )
        # room + owner membership + room_created (seq 1) + its outbox row: ONE transaction
        await record_room_event(
            db,
            adopt_new_room(room),
            event_type="room_created",
            resource_type="room",
            resource_id=room.id,
            resource_version=None,
            actor_id=user_id,
            payload={},
        )
    log_event("room_created", room.id, user_id, user_id)
    return to_view(room, "owner")


async def get_room_for_member(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, user_id: uuid.UUID
) -> RoomView:
    """Room details for an ACTIVE member. Anyone else (nonmember, removed) gets room_not_found."""
    async with sessionmaker() as db:
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
    return to_view(row, row.my_role)


async def list_rooms(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[RoomView], str | None]:
    """The caller's ACTIVE memberships joined to rooms, newest first, keyset-paginated."""
    after = decode_cursor(cursor)
    query = (
        select(_ROOMS, _MEMBERSHIPS.c.role.label("my_role"))
        .join(_MEMBERSHIPS, _MEMBERSHIPS.c.room_id == _ROOMS.c.id)
        .where(_MEMBERSHIPS.c.user_id == user_id, _MEMBERSHIPS.c.state == "active")
        .order_by(_ROOMS.c.created_at.desc(), _ROOMS.c.id.desc())
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(tuple_(_ROOMS.c.created_at, _ROOMS.c.id) < tuple_(*after))
    async with sessionmaker() as db:
        rows = (await db.execute(query)).all()
    page, extra = rows[:limit], rows[limit:]
    items = [to_view(r, r.my_role) for r in page]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if extra else None
    return items, next_cursor


async def rename_room(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    name: str,
) -> RoomView:
    """Owner-only rename. Authorization is decided HERE under the room lock; any dependency
    pre-check is only a fast rejection. A same-name PATCH changes nothing (no version bump).
    Never touches membership_epoch: a rename is not an access change."""
    changed = False
    async with sessionmaker() as db, db.begin():
        locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=True)
        room = locked.row
        if room.name != name:
            room = (
                await db.execute(
                    update(_ROOMS)
                    .where(_ROOMS.c.id == room_id)
                    .values(name=name, version=_ROOMS.c.version + 1, updated_at=func.now())
                    .returning(*_ROOMS.c)
                )
            ).one()
            await record_room_event(
                db,
                locked,
                event_type="room_renamed",
                resource_type="room",
                resource_id=room_id,
                resource_version=None,
                actor_id=actor_id,
                payload={},
            )
            changed = True
    if changed:
        log_event("room_renamed", room_id, actor_id, None)
    return to_view(room, role)
