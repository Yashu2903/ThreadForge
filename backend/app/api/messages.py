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

from fastapi import APIRouter, Request, Response

from app.dependencies.rooms import WriterAccessUnsafe
from app.request_body import json_body_docs, parse_json_body
from app.schemas.auth import ErrorBody
from app.schemas.messages import MessageOut, SendMessageRequest
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
