from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now_column, uuid_pk

USER_STATES = ("active", "disabled", "deleted")


class User(Base):
    """A ThreadForge account. Identity comes from auth_identities, never from `email`."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(display_name)) BETWEEN 1 AND 100", name="display_name_length"
        ),
        CheckConstraint(
            "email IS NULL OR char_length(email) BETWEEN 1 AND 320", name="email_length"
        ),
        CheckConstraint("state IN ('active', 'disabled', 'deleted')", name="state_valid"),
        CheckConstraint("version >= 0", name="version_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Profile metadata only: NOT unique, never used for lookup, matching or authorization.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), server_default=text("'active'"), nullable=False)
    # Optimistic-concurrency counter for future If-Match style updates.
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
