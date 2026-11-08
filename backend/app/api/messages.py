"""Message routes. Gate order (each stage only runs if the previous one passed):

  1. body-size middleware (413)            app/body_limit.py
  2. authentication (401)                  CurrentUser
  3. CSRF on unsafe methods (403)          require_csrf
  4. UNLOCKED membership pre-check         404 room_not_found / 403 insufficient_role
  5. request validation (422)              parse_json_body, run here AFTER the gates
  6. the transaction                       app/services/messages.py: room lock, recheck, write

The pre-check is a fast rejection so people without access never take the room lock; it is never
the authority (the service rechecks membership and role under the lock). The author is always the
authenticated session user: no request field can name one.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from app.dependencies.rooms import MemberAccess, WriterAccessUnsafe, parse_uuid
from app.errors import message_not_found
from app.request_body import json_body_docs, parse_json_body
from app.schemas.auth import ErrorBody
from app.schemas.messages import (
    MAX_BIGINT,
    EditMessageRequest,
    MessageOut,
    MessagePage,
    SendMessageRequest,
)
from app.services import messages

router = APIRouter(tags=["messages"])

_COMMON = {
    401: {"model": ErrorBody, "description": "unauthenticated"},
    403: {
        "model": ErrorBody,
        "description": "csrf_failed / insufficient_role / message_not_author",
    },
    404: {"model": ErrorBody, "description": "room_not_found (also for non-members)"},
}


@router.post(
    "/rooms/{room_id}/messages",
    response_model=MessageOut,
    status_code=201,
    responses={
        200: {"model": MessageOut, "description": "A matching retry: the existing message"},
        409: {"model": ErrorBody, "description": "message_id_reused"},
        413: {"model": ErrorBody, "description": "payload_too_large"},
        429: {"model": ErrorBody, "description": "rate_limited (Retry-After set)"},
        **_COMMON,
    },
    openapi_extra=json_body_docs(SendMessageRequest),
)
async def send_message(
    request: Request, response: Response, access: WriterAccessUnsafe
) -> MessageOut:
    """Owner or editor. 201 for a new message; 200 for a matching retry of the same
    client_message_id (reusing the id with different text is 409 message_id_reused)."""
    body = await parse_json_body(request, SendMessageRequest)
    result = await messages.send_message(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        body.client_message_id,
        body.body,
    )
    response.status_code = 201 if result.created else 200
    return MessageOut(**result.message.__dict__)


@router.get(
    "/rooms/{room_id}/messages",
    response_model=MessagePage,
    responses={
        **_COMMON,
        422: {"model": ErrorBody, "description": "invalid_cursor / validation_error"},
    },
)
async def list_messages(
    request: Request,
    access: MemberAccess,
    limit: int = Query(50, ge=1, le=100),
    before: str | None = None,
) -> MessagePage:
    """History for any active member (any role): the newest page first (oldest-to-newest within
    the page), then older pages via `before=<next_before>`. Includes tombstones, and messages
    written before the caller joined or by since-removed members (no per-member visibility window).
    No locks are taken."""
    items, next_before = await messages.list_messages(
        request.app.state.sessionmaker, access.room_id, limit, before
    )
    return MessagePage(items=[MessageOut(**m.__dict__) for m in items], next_before=next_before)


@router.get("/rooms/{room_id}/messages/{message_id}", response_model=MessageOut, responses=_COMMON)
async def get_message(request: Request, access: MemberAccess, message_id: str) -> MessageOut:
    """One message or tombstone, scoped by BOTH the room and the message id."""
    parsed = parse_uuid(message_id)
    view = (
        await messages.get_message(request.app.state.sessionmaker, access.room_id, parsed)
        if parsed
        else None
    )
    if view is None:
        raise message_not_found()
    return MessageOut(**view.__dict__)


@router.patch(
    "/rooms/{room_id}/messages/{message_id}",
    response_model=MessageOut,
    responses={
        409: {
            "model": ErrorBody,
            "description": "message_version_conflict (with current_version) / message_deleted",
        },
        429: {"model": ErrorBody, "description": "rate_limited (Retry-After set)"},
        **_COMMON,
    },
    openapi_extra=json_body_docs(EditMessageRequest),
)
async def edit_message(request: Request, access: WriterAccessUnsafe, message_id: str) -> MessageOut:
    """Author only (and the author must still be an active owner or editor). Send the version you
    last saw as expected_version; a stale version is 409 message_version_conflict."""
    body = await parse_json_body(request, EditMessageRequest)
    view = await messages.edit_message(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        parse_uuid(message_id),
        body.body,
        body.expected_version,
    )
    return MessageOut(**view.__dict__)


@router.delete(
    "/rooms/{room_id}/messages/{message_id}",
    status_code=204,
    response_class=Response,
    responses={
        409: {"model": ErrorBody, "description": "message_version_conflict"},
        429: {"model": ErrorBody, "description": "rate_limited (Retry-After set)"},
        **_COMMON,
    },
)
async def delete_message(
    request: Request,
    access: WriterAccessUnsafe,
    message_id: str,
    expected_version: int = Query(..., ge=1, le=MAX_BIGINT),
) -> Response:
    """Soft delete (tombstone). Owner: any message. Editor: only their own. Deleting an already
    deleted message is 204 regardless of expected_version."""
    await messages.delete_message(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        parse_uuid(message_id),
        expected_version,
    )
    return Response(status_code=204)
