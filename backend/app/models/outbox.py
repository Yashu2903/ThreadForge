from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OutboxEntry(Base):
    """Publication work for one room event: the transactional-outbox row.

    It is inserted in the SAME transaction as the business change and its room event, so a
    committed change can never lack its publication work, and a dispatcher crash cannot erase it.
    A separate dispatcher process claims rows and publishes a small notification to Redis;
    publication may happen more than once (clients de-duplicate by event_id).

    ONE composite foreign key (room_id, room_seq, event_id) -> room_events. Two independent
    foreign keys (event_id -> room_events, and (room_id, room_seq) -> room_events) would each be
    satisfied by a DIFFERENT room_events row, so an outbox row could name one event's id and
    another event's sequence. The composite key forces all three columns to come from the same row.

    attempts counts CLAIMS (including claims that were released or expired), not failures.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "room_seq", "event_id"],
            ["room_events.room_id", "room_events.seq", "room_events.event_id"],
            name="fk_outbox_room_event",
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind IN ('room_event')", name="kind_valid"),
        CheckConstraint(
            "state IN ('pending', 'processing', 'processed', 'failed')", name="state_valid"
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
        CheckConstraint(
            "(state = 'pending' AND claim_token IS NULL AND claimed_until IS NULL"
            " AND processed_at IS NULL)"
            " OR (state = 'processing' AND claim_token IS NOT NULL AND claimed_until IS NOT NULL"
            " AND processed_at IS NULL)"
            " OR (state = 'processed' AND processed_at IS NOT NULL AND claim_token IS NULL"
            " AND claimed_until IS NULL)"
            " OR (state = 'failed' AND claim_token IS NULL AND claimed_until IS NULL"
            " AND last_error_code IS NOT NULL)",
            name="state_fields_consistent",
        ),
        Index(
            "ix_outbox_pending_next_attempt_at",
            "next_attempt_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index(
            "ix_outbox_processing_claimed_until",
            "claimed_until",
            postgresql_where=text("state = 'processing'"),
        ),
        Index(
            "ix_outbox_processed_processed_at",
            "processed_at",
            postgresql_where=text("state = 'processed'"),
        ),
        Index(
            "ix_outbox_failed_created_at", "created_at", postgresql_where=text("state = 'failed'")
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    room_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), server_default=text("'room_event'"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
