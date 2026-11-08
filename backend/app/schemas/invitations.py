from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.rooms import MemberOut, RoomOut


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateInvitationRequest(_StrictRequest):
    # "owner" is not in the allowlist: an invitation can never grant ownership.
    role: Literal["editor", "viewer"] = "editor"
    # Bounds come from settings (INVITATION_MIN_HOURS / _MAX_HOURS), checked in the route.
    expires_in_hours: int | None = None


class RedeemRequest(_StrictRequest):
    # Deliberately a plain string: a malformed token must get the same 404 invitation_invalid as
    # an unknown one, not a 422 that tells an attacker which format rule they broke.
    token: str


class InvitationCreatedOut(BaseModel):
    """The ONLY response that ever contains the raw token."""

    id: uuid.UUID
    room_id: uuid.UUID
    role: str
    expires_at: datetime
    token: str
    url: str


class InvitationOut(BaseModel):
    """Metadata only: never the token or its hash."""

    id: uuid.UUID
    role: str
    created_by: uuid.UUID
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    used_by: uuid.UUID | None
    revoked_at: datetime | None
    status: Literal["active", "used", "revoked", "expired"]


class RedeemOut(BaseModel):
    room: RoomOut
    membership: MemberOut
