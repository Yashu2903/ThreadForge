from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now_column, uuid_pk


class Session(Base):
    """A browser login. The browser holds a random secret; only its SHA-256 is stored here."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="revoked_after_created"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = utc_now_column()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
