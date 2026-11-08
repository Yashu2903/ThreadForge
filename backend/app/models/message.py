from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class Message(Base):
    """A plain-text room message. Deleting turns it into a tombstone (body NULL); the row is never
    physically deleted and no foreign key cascades, so removing a member never removes messages.

    created_at has NO server default on purpose: it must be assigned explicitly with
    clock_timestamp() after the room lock is held (a now()-based default would be the time the
    transaction STARTED, which can be older than an already-committed message and make keyset
    pagination skip rows). Same for edited_at and deleted_at.

    Immutable after insert (enforced by trigger trg_messages_immutable, see migration 0003):
    room_id, author_id, client_message_id, initial_request_fingerprint, created_at.

    version starts at 1 (not 0 like the Part 1/2 tables) because clients echo it back as
    expected_version; 1 means "never edited".
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "room_id", "author_id", "client_message_id", name="uq_messages_room_author_client"
        ),
        CheckConstraint(
            "(deleted_at IS NULL AND body IS NOT NULL AND char_length(body) BETWEEN 1 AND 5000)"
            " OR (deleted_at IS NOT NULL AND body IS NULL)",
            name="body_matches_deletion",
        ),
        CheckConstraint(
            "(deleted_at IS NULL AND deleted_by IS NULL)"
            " OR (deleted_at IS NOT NULL AND deleted_by IS NOT NULL)",
            name="deleted_pair",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "octet_length(initial_request_fingerprint) = 32", name="fingerprint_length"
        ),
        Index("ix_messages_room_id_created_at_id", "room_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    client_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # HMAC-SHA256 of the creation request; never exposed by the API.
    initial_request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"), nullable=False)
