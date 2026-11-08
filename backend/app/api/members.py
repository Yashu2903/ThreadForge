"""Member management routes (owner only). Authorization is checked by a dependency for fast
rejection and again, authoritatively, under the room lock inside the service."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.dependencies.rooms import OwnerAccessUnsafe, parse_uuid
from app.schemas.auth import ErrorBody
from app.schemas.rooms import ChangeRoleRequest, MemberOut
from app.services import memberships

router = APIRouter(tags=["members"])

_ERRORS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody, "description": "insufficient_role / csrf_failed"},
    404: {"model": ErrorBody, "description": "room_not_found / member_not_found"},
    409: {"model": ErrorBody, "description": "owner_protected"},
}


@router.patch("/rooms/{room_id}/members/{user_id}", response_model=MemberOut, responses=_ERRORS)
async def change_member_role(
    body: ChangeRoleRequest, request: Request, access: OwnerAccessUnsafe, user_id: str
) -> MemberOut:
    view = await memberships.change_role(
        request.app.state.sessionmaker,
        access.user_id,
        access.room_id,
        parse_uuid(user_id),
        body.role,
    )
    return MemberOut(**view.__dict__)


@router.delete(
    "/rooms/{room_id}/members/{user_id}",
    status_code=204,
    response_class=Response,
    responses=_ERRORS,
)
async def remove_member(request: Request, access: OwnerAccessUnsafe, user_id: str) -> Response:
    await memberships.remove_member(
        request.app.state.sessionmaker, access.user_id, access.room_id, parse_uuid(user_id)
    )
    return Response(status_code=204)
