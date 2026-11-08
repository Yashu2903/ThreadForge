"""Invitation routes. The raw token is returned once (creation) and accepted once (redemption);
the link carries it in the URL FRAGMENT (/join#token=...), which browsers never send to servers, so
it stays out of access logs, Referer headers and proxy logs. A GET to the link never joins a room:
joining is an authenticated, CSRF-protected POST /invitations/redeem. The future join page must
read the fragment, then remove it from the address bar (history.replaceState)."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from app.dependencies.auth import CsrfProtectedUser
from app.dependencies.rooms import OwnerAccess, OwnerAccessUnsafe, parse_uuid
from app.errors import validation_failed
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page
from app.schemas.auth import ErrorBody
from app.schemas.invitations import (
    CreateInvitationRequest,
    InvitationCreatedOut,
    InvitationOut,
    RedeemOut,
    RedeemRequest,
)
from app.schemas.rooms import MemberOut, RoomOut
from app.services import invitations

router = APIRouter(tags=["invitations"])

_OWNER_ERRORS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody, "description": "insufficient_role / csrf_failed"},
    404: {"model": ErrorBody, "description": "room_not_found"},
}


@router.post(
    "/rooms/{room_id}/invitations",
    status_code=201,
    response_model=InvitationCreatedOut,
    responses={
        **_OWNER_ERRORS,
        409: {"model": ErrorBody, "description": "invitation_limit_reached"},
    },
)
async def create_invitation(
    request: Request, access: OwnerAccessUnsafe, body: CreateInvitationRequest | None = None
) -> InvitationCreatedOut:
    """Owner only. Returns the raw token ONCE; only its hash is stored."""
    settings = request.app.state.settings
    body = body or CreateInvitationRequest()
    hours = (
        body.expires_in_hours
        if body.expires_in_hours is not None
        else settings.invitation_default_hours
    )
    if not settings.invitation_min_hours <= hours <= settings.invitation_max_hours:
        raise validation_failed(
            f"expires_in_hours must be between {settings.invitation_min_hours} "
            f"and {settings.invitation_max_hours}."
        )
    created = await invitations.create_invitation(
        request.app.state.sessionmaker, settings, access.user_id, access.room_id, body.role, hours
    )
    return InvitationCreatedOut(
        id=created.id,
        room_id=created.room_id,
        role=created.role,
        expires_at=created.expires_at,
        token=created.token,
        # Built from configuration only; no client-supplied destination exists.
        url=f"{settings.frontend_base_url}/join#token={created.token}",
    )


@router.get(
    "/rooms/{room_id}/invitations", response_model=Page[InvitationOut], responses=_OWNER_ERRORS
)
async def list_invitations(
    request: Request,
    access: OwnerAccess,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
) -> Page[InvitationOut]:
    items, next_cursor = await invitations.list_invitations(
        request.app.state.sessionmaker, access.room_id, limit, cursor
    )
    return Page(items=[InvitationOut(**i.__dict__) for i in items], next_cursor=next_cursor)


@router.delete(
    "/rooms/{room_id}/invitations/{invitation_id}",
    status_code=204,
    response_class=Response,
    responses={
        **_OWNER_ERRORS,
        409: {"model": ErrorBody, "description": "invitation_already_redeemed"},
    },
)
async def revoke_invitation(
    request: Request, access: OwnerAccessUnsafe, invitation_id: str
) -> Response:
    await invitations.revoke_invitation(
        request.app.state.sessionmaker, access.user_id, access.room_id, parse_uuid(invitation_id)
    )
    return Response(status_code=204)


@router.post(
    "/invitations/redeem",
    response_model=RedeemOut,
    responses={
        401: {"model": ErrorBody},
        403: {"model": ErrorBody, "description": "csrf_failed"},
        404: {"model": ErrorBody, "description": "invitation_invalid (every failure reason)"},
        409: {"model": ErrorBody, "description": "member_limit_reached"},
    },
)
async def redeem_invitation(
    body: RedeemRequest, request: Request, user: CsrfProtectedUser
) -> RedeemOut:
    """Authenticated + CSRF-protected. The room and role come ONLY from the invitation row."""
    result = await invitations.redeem_invitation(
        request.app.state.sessionmaker, request.app.state.settings, user.user.id, body.token
    )
    return RedeemOut(
        room=RoomOut(**result.room.__dict__), membership=MemberOut(**result.membership.__dict__)
    )
