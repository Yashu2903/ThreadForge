"""GET /rooms/{room_id}/sync: consistent initial synchronization (see app/services/sync.py)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.dependencies.rooms import MemberAccess
from app.schemas.auth import ErrorBody
from app.schemas.sync import SyncOut
from app.services.sync import build_sync

router = APIRouter(tags=["sync"])


@router.get(
    "/rooms/{room_id}/sync",
    response_model=SyncOut,
    responses={
        401: {"model": ErrorBody},
        404: {"model": ErrorBody, "description": "room_not_found (also for non-members)"},
    },
)
async def sync_room(request: Request, access: MemberAccess) -> SyncOut:
    """Room, caller's role, members, the latest 50 messages and `through_seq`, from ONE snapshot.
    The client installs all of it together with the cursor, opens the socket and sends
    resume{after_seq: through_seq}; events committed since are replayed from PostgreSQL."""
    return await build_sync(
        request.app.state.sessionmaker, request.app.state.settings, access.room_id, access.user_id
    )
