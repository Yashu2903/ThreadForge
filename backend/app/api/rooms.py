"""Room routes. Thin: authentication, CSRF and room authorization come from dependencies; all
database work (and the authoritative authorization re-check) happens in app/services/."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.dependencies.auth import CsrfProtectedUser, CurrentUser
from app.dependencies.rooms import MemberAccess, OwnerAccessUnsafe
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page
from app.schemas.auth import ErrorBody
from app.schemas.rooms import CreateRoomRequest, MemberOut, RenameRoomRequest, RoomOut
from app.services import memberships, rooms

router = APIRouter(tags=["rooms"])

_AUTH_ERRORS = {
    401: {"model": ErrorBody, "description": "unauthenticated"},
    404: {"model": ErrorBody, "description": "room_not_found (also for non-members)"},
}


def _room_out(view: rooms.RoomView) -> RoomOut:
    return RoomOut(**view.__dict__)


@router.post(
    "/rooms", status_code=201, response_model=RoomOut, responses={409: {"model": ErrorBody}}
)
async def create_room(
    body: CreateRoomRequest, request: Request, user: CsrfProtectedUser
) -> RoomOut:
    """Create a room; the caller becomes its owner (room + owner membership in one transaction)."""
    view = await rooms.create_room(
        request.app.state.sessionmaker, request.app.state.settings, user.user.id, body.name
    )
    return _room_out(view)


@router.get("/rooms", response_model=Page[RoomOut])
async def list_my_rooms(
    request: Request,
    user: CurrentUser,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
) -> Page[RoomOut]:
    """Rooms where the caller has an ACTIVE membership, newest first."""
    items, next_cursor = await rooms.list_rooms(
        request.app.state.sessionmaker, user.user.id, limit, cursor
    )
    return Page(items=[_room_out(v) for v in items], next_cursor=next_cursor)


@router.get("/rooms/{room_id}", response_model=RoomOut, responses=_AUTH_ERRORS)
async def get_room(request: Request, access: MemberAccess) -> RoomOut:
    view = await rooms.get_room_for_member(
        request.app.state.sessionmaker, access.room_id, access.user_id
    )
    return _room_out(view)


@router.patch("/rooms/{room_id}", response_model=RoomOut, responses=_AUTH_ERRORS)
async def rename_room(
    body: RenameRoomRequest, request: Request, access: OwnerAccessUnsafe
) -> RoomOut:
    """Owner only; accepts only `name`. Does not change membership_epoch."""
    view = await rooms.rename_room(
        request.app.state.sessionmaker, access.user_id, access.room_id, body.name
    )
    return _room_out(view)


@router.get("/rooms/{room_id}/members", response_model=Page[MemberOut], responses=_AUTH_ERRORS)
async def list_room_members(
    request: Request,
    access: MemberAccess,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
) -> Page[MemberOut]:
    items, next_cursor = await memberships.list_members(
        request.app.state.sessionmaker, access.room_id, limit, cursor
    )
    return Page(items=[MemberOut(**m.__dict__) for m in items], next_cursor=next_cursor)
