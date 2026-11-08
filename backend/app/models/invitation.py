from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now_column, uuid_pk


class Invitation(Base):
    """A single-use bearer credential granting one role in one room. Only its SHA-256 is stored.

    Deliberately no CHECKs relating revoked_at/used_at to created_at (different transactions,
    non-monotonic database clocks: see the Part 1 note). expires_at > created_at is safe because
    both values come from one statement.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
        CheckConstraint("role IN ('editor', 'viewer')", name="role_valid"),
        CheckConstraint(
            "(used_at IS NULL AND used_by IS NULL)"
            " OR (used_at IS NOT NULL AND used_by IS NOT NULL)",
            name="used_pair",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_invitations_room_id_created_at_id", "room_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = utc_now_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
