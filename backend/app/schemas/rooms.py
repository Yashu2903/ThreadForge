"""Request and response schemas for rooms. Requests are strict and reject unknown fields, so a
client cannot smuggle in owner_id, actor ids, roles on routes that do not take them, etc."""

from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

ROOM_NAME_MAX = 120


def _clean_room_name(value: str) -> str:
    trimmed = value.strip()
    if not 1 <= len(trimmed) <= ROOM_NAME_MAX:
        raise ValueError(f"name must be 1-{ROOM_NAME_MAX} characters after trimming")
    if any(unicodedata.category(ch).startswith("C") for ch in trimmed):
        raise ValueError("name must not contain control characters")
    return trimmed


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateRoomRequest(_StrictRequest):
    name: str

    _clean = field_validator("name")(_clean_room_name)


class RenameRoomRequest(_StrictRequest):
    name: str

    _clean = field_validator("name")(_clean_room_name)


class RoomOut(BaseModel):
    """Room details as seen by an active member. `role` is the CALLER's role."""

    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    role: str
    membership_epoch: int
    created_at: datetime
    updated_at: datetime
    # Part 6: the room's one shared active document (null until one is created or chosen)
    active_document_id: uuid.UUID | None = None
    active_document_version: int = 0


class MemberOut(BaseModel):
    """No email or provider data: only what a room member needs to see."""

    user_id: uuid.UUID
    display_name: str
    role: str
    joined_at: datetime


class ChangeRoleRequest(_StrictRequest):
    # "owner" is not in the allowlist: a role update can never create an owner.
    role: Literal["editor", "viewer"]
