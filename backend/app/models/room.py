from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now_column, uuid_pk


class Room(Base):
    """A private room. Not discoverable: access is by membership only.

    membership_epoch is an access-policy version for future live connections and caches. It is
    bumped (under the room lock) only when access actually changes. version follows the Part 1
    convention: it is bumped on every real UPDATE of the row (rename or epoch bump).
    """

    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 120", name="name_valid"
        ),
        CheckConstraint("membership_epoch >= 1", name="epoch_positive"),
        CheckConstraint("version >= 0", name="version_non_negative"),
        CheckConstraint("event_seq >= 0", name="event_seq_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    membership_epoch: Mapped[int] = mapped_column(
        BigInteger, server_default=text("1"), nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    # Last allocated room_events.seq. Separate from membership_epoch (access-policy version) and
    # from version (entity version); bumping it changes neither them nor updated_at.
    event_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
