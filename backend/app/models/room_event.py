from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

EVENT_TYPES = (
    "room_created",
    "room_renamed",
    "member_joined",
    "member_role_changed",
    "member_removed",
    "message_created",
    "message_edited",
    "message_deleted",
    "document_created",
    "document_renamed",
    "active_document_changed",
    "document_content_updated",
    "room_ai_settings_changed",
)
_TYPE_LIST = ", ".join(f"'{t}'" for t in EVENT_TYPES)


class RoomEvent(Base):
    """A durable, append-only record that something changed in a room.

    The application only ever INSERTs here (never updates or deletes). `seq` is allocated by
    `UPDATE rooms SET event_seq = event_seq + 1 ... RETURNING` while the room row is locked, so
    sequences are contiguous (1, 2, 3, ...) and committed in sequence order. An event is a CHANGE
    NOTIFICATION, not a copy: a later fetch of the message may show a newer version.

    created_at has no server default: it is assigned with clock_timestamp() inside the caller's
    transaction (as for messages), never with a now()-based default.
    """

    __tablename__ = "room_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_room_events_event_id"),
        # Exists so outbox can reference (room_id, seq, event_id) with ONE composite foreign key.
        UniqueConstraint("room_id", "seq", "event_id", name="uq_room_events_room_seq_event"),
        CheckConstraint("seq > 0", name="seq_positive"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint(f"type IN ({_TYPE_LIST})", name="type_valid"),
        CheckConstraint(
            "resource_type IN ('room', 'membership', 'message', 'document')",
            name="resource_type_valid",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
        CheckConstraint("octet_length(payload::text) <= 2048", name="payload_size"),
    )

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()"), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resource_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
