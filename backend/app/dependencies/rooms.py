"""Room authorization dependencies.

Layering for every room route: authentication (CurrentUser, 401) -> CSRF on unsafe methods
(CsrfProtectedUser, 403) -> room access (here: 404 room_not_found / 403 insufficient_role) ->
request-body validation (422) -> the service function, which RE-CHECKS authorization under the
room lock. These dependencies are fast rejections only; they are never the authority for a
mutation (a role could change between this read and the write).

FastAPI resolves dependencies before it raises body-validation errors, which is what makes a
nonmember sending an invalid body get 404, not 422. Room ids are taken as plain strings here so a
malformed id is room_not_found (404), not a validation error that would reveal the id was parsed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.dependencies.auth import CsrfProtectedUser, CurrentUser
from app.errors import insufficient_role, room_not_found
from app.services.memberships import get_active_role
from app.services.sessions import AuthContext


@dataclass(frozen=True)
class RoomAccess:
    room_id: uuid.UUID
    user_id: uuid.UUID  # always from the session, never from the request
    role: str


def parse_uuid(value: str) -> uuid.UUID | None:
    """Strict canonical UUID (hyphenated, case-insensitive); anything else is None."""
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    return parsed if str(parsed) == value.lower() else None


async def _check(
    request: Request, room_id: str, auth: AuthContext, *, owner_only: bool
) -> RoomAccess:
    parsed = parse_uuid(room_id)
    if parsed is None:
        raise room_not_found()
    role = await get_active_role(request.app.state.sessionmaker, parsed, auth.user.id)
    if role is None:
        raise room_not_found()
    if owner_only and role != "owner":
        raise insufficient_role()
    return RoomAccess(room_id=parsed, user_id=auth.user.id, role=role)


async def require_room_member(room_id: str, request: Request, user: CurrentUser) -> RoomAccess:
    """Safe (GET) routes: any active member."""
    return await _check(request, room_id, user, owner_only=False)


async def require_room_owner(room_id: str, request: Request, user: CurrentUser) -> RoomAccess:
    """Safe (GET) routes: owner only."""
    return await _check(request, room_id, user, owner_only=True)


async def require_room_member_unsafe(
    room_id: str, request: Request, user: CsrfProtectedUser
) -> RoomAccess:
    """Unsafe routes: CSRF first, then any active member."""
    return await _check(request, room_id, user, owner_only=False)


async def require_room_owner_unsafe(
    room_id: str, request: Request, user: CsrfProtectedUser
) -> RoomAccess:
    """Unsafe routes: CSRF first, then owner only."""
    return await _check(request, room_id, user, owner_only=True)


MemberAccess = Annotated[RoomAccess, Depends(require_room_member)]
OwnerAccess = Annotated[RoomAccess, Depends(require_room_owner)]
MemberAccessUnsafe = Annotated[RoomAccess, Depends(require_room_member_unsafe)]
OwnerAccessUnsafe = Annotated[RoomAccess, Depends(require_room_owner_unsafe)]


async def require_room_writer_unsafe(
    room_id: str, request: Request, user: CsrfProtectedUser
) -> RoomAccess:
    """Unsafe message routes: CSRF, then an active member, then owner or editor (a viewer gets
    403 insufficient_role). Fast, UNLOCKED rejection so that people with no write access can never
    take the room lock; never the authority (the service re-checks under the lock)."""
    access = await _check(request, room_id, user, owner_only=False)
    if access.role not in ("owner", "editor"):
        raise insufficient_role()
    return access


WriterAccessUnsafe = Annotated[RoomAccess, Depends(require_room_writer_unsafe)]
