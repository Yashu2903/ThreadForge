from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MessageWriteBucket(Base):
    """Fixed-window write counter per (room, user, minute), in PostgreSQL so every API process
    shares it and a rolled-back write consumes nothing (the increment is in the same transaction
    as the message change)."""

    __tablename__ = "message_write_buckets"
    __table_args__ = (
        CheckConstraint("count >= 0", name="count_non_negative"),
        # Lets the cleanup command find old windows without scanning the table.
        Index("ix_message_write_buckets_window_start", "window_start"),
    )

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
