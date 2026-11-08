"""Room-event schemas: the supported types, their strict payloads, and the shared wire shape.

Events are CHANGE NOTIFICATIONS, not copies of data. A message_edited event says "message M reached
version 3"; fetching M later may show version 5. Events never contain message text (live or
deleted), invitation tokens or hashes, emails, session or provider credentials, or request bodies:
a payload holds only ids and roles, validated here (extra fields forbidden) before it is inserted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EVENT_SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 2048


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyPayload(_Strict):
    pass


class MemberPayload(_Strict):
    user_id: uuid.UUID
    role: Literal["owner", "editor", "viewer"]


class MemberRemovedPayload(_Strict):
    user_id: uuid.UUID


@dataclass(frozen=True)
class EventSpec:
    resource_type: str
    versioned: bool  # message events carry the resource_version; others must not
    payload_model: type[_Strict]


EVENT_SPECS: dict[str, EventSpec] = {
    "room_created": EventSpec("room", False, EmptyPayload),
    "room_renamed": EventSpec("room", False, EmptyPayload),
    "member_joined": EventSpec("membership", False, MemberPayload),
    "member_role_changed": EventSpec("membership", False, MemberPayload),
    "member_removed": EventSpec("membership", False, MemberRemovedPayload),
    "message_created": EventSpec("message", True, EmptyPayload),
    "message_edited": EventSpec("message", True, EmptyPayload),
    "message_deleted": EventSpec("message", True, EmptyPayload),
}


def validate_event_fields(
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    resource_version: int | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the JSON-safe payload, or raise ValueError. Called before every insert."""
    spec = EVENT_SPECS.get(event_type)
    if spec is None:
        raise ValueError(f"unsupported event type {event_type!r}")
    if resource_type != spec.resource_type:
        raise ValueError(f"{event_type} must have resource_type {spec.resource_type!r}")
    if resource_id is None:
        raise ValueError(f"{event_type} requires a resource_id")
    if spec.versioned != (resource_version is not None):
        raise ValueError(
            f"{event_type} resource_version must be {'set' if spec.versioned else 'null'}"
        )
    if resource_version is not None and resource_version < 1:
        raise ValueError("resource_version must be >= 1")
    clean = spec.payload_model.model_validate(payload).model_dump(mode="json")
    return clean


class RoomEventOut(BaseModel):
    """THE event shape: used verbatim by the HTTP history endpoint (items) and, with a leading
    `type`, by WebSocket room_event frames."""

    schema_version: int
    event_id: uuid.UUID
    room_id: uuid.UUID
    room_seq: int
    event_type: str
    resource_type: str
    resource_id: uuid.UUID | None
    resource_version: int | None
    actor_id: uuid.UUID
    payload: dict[str, Any]
    created_at: datetime


class RoomEventFrame(BaseModel):
    """WebSocket frame: field order is part of the documented wire format."""

    type: Literal["room_event"] = "room_event"
    schema_version: int
    event_id: uuid.UUID
    room_id: uuid.UUID
    room_seq: int
    event_type: str
    resource_type: str
    resource_id: uuid.UUID | None
    resource_version: int | None
    payload: dict[str, Any]
    actor_id: uuid.UUID
    created_at: datetime


class ReadyFrame(BaseModel):
    """Sent first on every connection. Means "live subscription established", NOT "snapshot
    consistent" or "replay complete". current_room_seq is the high-water mark when the
    subscription became live; it is the starting point for Part 5's replay."""

    type: Literal["ready"] = "ready"
    schema_version: int = EVENT_SCHEMA_VERSION
    room_id: uuid.UUID
    current_room_seq: int


class OutboxEnvelope(BaseModel):
    """The tiny notification published to Redis (and stored in outbox.payload). It carries ids
    only; receivers re-read the event from PostgreSQL."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    event_id: uuid.UUID
    room_id: uuid.UUID
    room_seq: int = Field(gt=0)


class EventsPage(BaseModel):
    items: list[RoomEventOut]
    next_after_seq: int
    has_more: bool
    current_room_seq: int
