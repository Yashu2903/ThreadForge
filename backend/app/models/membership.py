from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

ROLES = ("owner", "editor", "viewer")


class Membership(Base):
    """A user's access to a room. Rows are retained after removal and reactivated on rejoin.

    DB-enforced: at most one *active* owner per room (partial unique index) and an owner row is
    never marked removed. Not DB-enforced (service layer, under the room lock): exactly one owner
    row exists and it matches rooms.owner_id.
    """

    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="role_valid"),
        CheckConstraint("state IN ('active', 'removed')", name="state_valid"),
        CheckConstraint(
            "(state = 'active' AND removed_at IS NULL)"
            " OR (state = 'removed' AND removed_at IS NOT NULL)",
            name="removed_at_matches_state",
        ),
        CheckConstraint("NOT (role = 'owner' AND state = 'removed')", name="owner_not_removed"),
        Index("ix_memberships_user_id_state", "user_id", "state"),
        Index(
            "uq_memberships_one_active_owner",
            "room_id",
            unique=True,
            postgresql_where=text("role = 'owner' AND state = 'active'"),
        ),
    )

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), server_default=text("'active'"), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
