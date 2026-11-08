"""Durable event history over HTTP:  GET /rooms/{room_id}/events?after_seq=41&limit=100

PostgreSQL is authoritative; this endpoint works with Redis down. Active members only (404
room_not_found for everyone else). Items are in ascending room_seq order using the same event shape
as WebSocket room_event frames (minus "type"). Part 4 does not prune events; retention and
old-cursor handling belong with recovery in Part 5.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.dependencies.rooms import MemberAccess
from app.schemas.auth import ErrorBody
from app.schemas.events import EventsPage
from app.schemas.messages import MAX_BIGINT
from app.services import room_events

router = APIRouter(tags=["events"])


@router.get(
    "/rooms/{room_id}/events",
    response_model=EventsPage,
    responses={
        401: {"model": ErrorBody},
        404: {"model": ErrorBody, "description": "room_not_found (also for non-members)"},
        409: {"model": ErrorBody, "description": "cursor_ahead (includes current_room_seq)"},
    },
)
async def list_room_events(
    request: Request,
    access: MemberAccess,
    after_seq: int = Query(0, ge=0, le=MAX_BIGINT),
    limit: int = Query(100, ge=1, le=500),
) -> EventsPage:
    """Events with room_seq > after_seq, oldest first. Out-of-range limit is a 422, never clamped.
    after_seq beyond the room's latest event is 409 cursor_ahead (this also happens after a
    database restore). items and current_room_seq come from ONE statement, so they agree."""
    items, next_after_seq, has_more, current = await room_events.list_events(
        request.app.state.sessionmaker, access.room_id, after_seq, limit
    )
    return EventsPage(
        items=items, next_after_seq=next_after_seq, has_more=has_more, current_room_seq=current
    )
