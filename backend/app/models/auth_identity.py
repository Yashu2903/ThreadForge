from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now_column, uuid_pk


class AuthIdentity(Base):
    """Links a provider account, identified by (issuer, subject), to one local user."""

    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_auth_identities_issuer_subject"),
        CheckConstraint(
            "issuer LIKE 'https://%' AND char_length(issuer) <= 255", name="issuer_valid"
        ),
        CheckConstraint("char_length(subject) BETWEEN 1 AND 255", name="subject_length"),
        CheckConstraint(
            "provider_email IS NULL OR char_length(provider_email) BETWEEN 1 AND 320",
            name="provider_email_length",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    # Refreshed on every login; informational only.
    provider_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    provider_email_verified: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
