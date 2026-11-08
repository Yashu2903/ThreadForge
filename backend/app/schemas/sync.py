"""GET /rooms/{room_id}/sync: the bootstrap a client installs together with its event cursor."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.messages import MessageOut
from app.schemas.rooms import MemberOut


class SyncRoom(BaseModel):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    active_document_id: uuid.UUID | None
    active_document_version: int


class SyncOut(BaseModel):
    """Everything here comes from ONE database snapshot, so `through_seq` counts exactly the
    changes the rest of the body already reflects. A client installs the state and the cursor
    together and then resumes the event stream with after_seq = through_seq."""

    room: SyncRoom
    role: str  # the caller's role
    membership_epoch: int
    members: list[MemberOut]  # active members only; no emails
    messages: list[MessageOut]  # newest 50, oldest first, tombstones included
    next_before: str | None  # Part 3 history cursor for older messages
    through_seq: int  # rooms.event_seq in the same snapshot (0 for rooms with no events yet)
