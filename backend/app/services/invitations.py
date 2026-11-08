"""Invitations: create, list, revoke, redeem.

An invitation is a BEARER credential: whoever holds the token (and is authenticated) may redeem it
once. It is not tied to an email address. Only SHA-256(token) is stored; the raw token is returned
exactly once, in the creation response. Locks: room, then invitation, then membership rows (see
app/services/rooms.py). No network calls happen inside any transaction here.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import (
    invitation_already_redeemed,
    invitation_invalid,
    invitation_not_found,
    limit_reached,
)
from app.models import Invitation, Membership, Room
from app.pagination import decode_cursor, encode_cursor
from app.services.memberships import (
    MemberView,
    activate_membership,
    lock_membership,
    member_view,
)
from app.services.room_events import record_room_event
from app.services.room_lock import lock_room
from app.services.rooms import (
    RoomView,
    bump_membership_epoch,
    lock_room_and_actor,
    log_event,
    to_view,
)
from app.services.security import new_secret, sha256_hex

_INVITATIONS = Invitation.__table__
_MEMBERSHIPS = Membership.__table__
_ROOMS = Room.__table__

# token_urlsafe(32): 32 random bytes -> exactly 43 URL-safe base64 characters, no padding.
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class InvitationView:
    id: uuid.UUID
    role: str
    created_by: uuid.UUID
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    used_by: uuid.UUID | None
    revoked_at: datetime | None
    status: str


@dataclass(frozen=True)
class CreatedInvitation:
    id: uuid.UUID
    room_id: uuid.UUID
    role: str
    expires_at: datetime
    token: str  # raw bearer token: shown once, never stored


@dataclass(frozen=True)
class RedeemResult:
    room: RoomView
    membership: MemberView


# Status, computed by the DATABASE clock: used, else revoked, else expired, else active.
_STATUS = case(
    (_INVITATIONS.c.used_at.is_not(None), "used"),
    (_INVITATIONS.c.revoked_at.is_not(None), "revoked"),
    (_INVITATIONS.c.expires_at <= func.now(), "expired"),
    else_="active",
).label("status")


async def create_invitation(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    role: str,
    expires_in_hours: int,
) -> CreatedInvitation:
    """Owner-only (checked here, under the room lock). The active-invitation cap is counted under
    the same lock, so concurrent creates cannot exceed it. Does NOT change membership_epoch."""
    token = new_secret(32)
    async with sessionmaker() as db, db.begin():
        await lock_room_and_actor(db, room_id, actor_id, owner_only=True)
        active = (
            await db.execute(
                select(func.count())
                .select_from(_INVITATIONS)
                .where(
                    _INVITATIONS.c.room_id == room_id,
                    _INVITATIONS.c.used_at.is_(None),
                    _INVITATIONS.c.revoked_at.is_(None),
                    _INVITATIONS.c.expires_at > func.now(),
                )
            )
        ).scalar_one()
        if active >= settings.max_active_invitations_per_room:
            raise limit_reached(
                "invitation_limit_reached", "This room has too many active invitations."
            )
        row = (
            await db.execute(
                insert(_INVITATIONS)
                .values(
                    room_id=room_id,
                    token_hash=sha256_hex(token),
                    role=role,
                    created_by=actor_id,
                    expires_at=func.now() + timedelta(hours=expires_in_hours),
                )
                .returning(_INVITATIONS.c.id, _INVITATIONS.c.expires_at)
            )
        ).one()
    log_event("invitation_created", room_id, actor_id, None)
    return CreatedInvitation(
        id=row.id, room_id=room_id, role=role, expires_at=row.expires_at, token=token
    )


async def list_invitations(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[InvitationView], str | None]:
    """Metadata only: never raw tokens or hashes. Scoped to the room."""
    after = decode_cursor(cursor)
    query = (
        select(
            _INVITATIONS.c.id,
            _INVITATIONS.c.role,
            _INVITATIONS.c.created_by,
            _INVITATIONS.c.created_at,
            _INVITATIONS.c.expires_at,
            _INVITATIONS.c.used_at,
            _INVITATIONS.c.used_by,
            _INVITATIONS.c.revoked_at,
            _STATUS,
        )
        .where(_INVITATIONS.c.room_id == room_id)
        .order_by(_INVITATIONS.c.created_at.desc(), _INVITATIONS.c.id.desc())
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(tuple_(_INVITATIONS.c.created_at, _INVITATIONS.c.id) < tuple_(*after))
    async with sessionmaker() as db:
        rows = (await db.execute(query)).all()
    page, extra = rows[:limit], rows[limit:]
    items = [InvitationView(**r._mapping) for r in page]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if extra else None
    return items, next_cursor


async def revoke_invitation(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    invitation_id: uuid.UUID | None,
) -> None:
    """Owner-only. Unused -> set revoked_at (idempotent; an expired-unused one may be revoked too).
    Used -> 409. Not in THIS room -> 404 (the lookup is scoped by room_id, so another room's
    invitation id is indistinguishable from a nonexistent one). Keeps the row."""
    if invitation_id is None:
        raise invitation_not_found()
    changed = False
    async with sessionmaker() as db, db.begin():
        await lock_room_and_actor(db, room_id, actor_id, owner_only=True)
        invitation = (
            await db.execute(
                select(_INVITATIONS.c.used_at, _INVITATIONS.c.revoked_at)
                .where(_INVITATIONS.c.id == invitation_id, _INVITATIONS.c.room_id == room_id)
                .with_for_update()
            )
        ).one_or_none()
        if invitation is None:
            raise invitation_not_found()
        if invitation.used_at is not None:
            raise invitation_already_redeemed()
        if invitation.revoked_at is None:
            await db.execute(
                update(_INVITATIONS)
                .where(_INVITATIONS.c.id == invitation_id)
                .values(revoked_at=func.now())
            )
            changed = True
    if changed:
        log_event("invitation_revoked", room_id, actor_id, None)


async def redeem_invitation(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    token: str,
) -> RedeemResult:
    """Redeem a token for the authenticated caller. Every failure is the SAME invitation_invalid.

    Let U be the caller, I the invitation, M the caller's membership row in I's room. After the
    locks (room -> invitation -> membership) are held, evaluate IN THIS ORDER:
      1. no invitation matches the hash (or the token is malformed)  -> invalid
      2. I.used_by == U: M active -> return M unchanged (a retry; holds even if I has expired);
                         M removed -> invalid (they need a new invitation)
      3. I.used_at set (by someone else)                              -> invalid
      4. I revoked, or expires_at <= database now()                   -> invalid
      5. M active (any role, even owner) -> return M; invitation NOT consumed, epoch untouched
      6. otherwise: member cap check, create/reactivate M with I.role, consume I, bump epoch.
    """
    if not TOKEN_FORMAT.fullmatch(token):  # validated BEFORE hashing; same answer as unknown
        raise invitation_invalid()
    token_hash = sha256_hex(token)

    joined_action: str | None = None
    async with sessionmaker() as db, db.begin():
        # Unlocked lookup, only to learn which room to lock. Nothing below trusts it.
        preliminary = (
            await db.execute(
                select(_INVITATIONS.c.id, _INVITATIONS.c.room_id).where(
                    _INVITATIONS.c.token_hash == token_hash
                )
            )
        ).one_or_none()
        if preliminary is None:
            raise invitation_invalid()

        # Lock order: 1) room  2) invitation  3) membership.
        locked = await lock_room(db, preliminary.room_id)
        if locked is None:  # cannot happen: foreign keys are RESTRICT
            raise invitation_invalid()
        room = locked.row
        invitation = (
            await db.execute(
                select(_INVITATIONS, (_INVITATIONS.c.expires_at <= func.now()).label("expired"))
                .where(
                    _INVITATIONS.c.id == preliminary.id,
                    _INVITATIONS.c.room_id == room.id,
                    _INVITATIONS.c.token_hash == token_hash,
                )
                .with_for_update()
            )
        ).one_or_none()
        if invitation is None:
            raise invitation_invalid()
        membership = await lock_membership(db, room.id, user_id)
        is_active = membership is not None and membership.state == "active"

        if invitation.used_by == user_id:  # rule 2
            if not is_active:
                raise invitation_invalid()
            return RedeemResult(to_view(room, membership.role), await member_view(db, membership))
        if invitation.used_at is not None:  # rule 3
            raise invitation_invalid()
        if invitation.revoked_at is not None or invitation.expired:  # rule 4
            raise invitation_invalid()
        if is_active:  # rule 5: already a member; do not consume, change role, or touch the epoch
            return RedeemResult(to_view(room, membership.role), await member_view(db, membership))

        # rule 6
        active_members = (
            await db.execute(
                select(func.count())
                .select_from(_MEMBERSHIPS)
                .where(_MEMBERSHIPS.c.room_id == room.id, _MEMBERSHIPS.c.state == "active")
            )
        ).scalar_one()
        if active_members >= settings.max_members_per_room:
            raise limit_reached(
                "member_limit_reached", "This room has reached its member limit."
            )  # invitation untouched: the transaction rolls back
        joined_action = "member_rejoined" if membership is not None else "member_joined"
        new_membership = await activate_membership(
            db, room.id, user_id, invitation.role, membership
        )
        await db.execute(
            update(_INVITATIONS)
            .where(_INVITATIONS.c.id == invitation.id)
            .values(used_at=func.now(), used_by=user_id)
        )
        epoch = await bump_membership_epoch(db, room.id)
        await record_room_event(
            db,
            locked,
            event_type="member_joined",
            resource_type="membership",
            resource_id=user_id,
            resource_version=None,
            actor_id=user_id,
            payload={"user_id": str(user_id), "role": new_membership.role},
        )
        view = to_view(room, new_membership.role)
        result = RedeemResult(
            RoomView(**{**view.__dict__, "membership_epoch": epoch}),
            await member_view(db, new_membership),
        )
    log_event(joined_action or "member_joined", room.id, user_id, user_id)
    return result
