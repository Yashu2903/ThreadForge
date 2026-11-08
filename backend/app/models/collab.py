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
    LargeBinary,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

SNAPSHOT_KINDS = ("seed", "periodic", "final", "restore")


class DocumentYjsState(Base):
    """The full Yjs state of a 'ready' document: the ONLY writable copy of its content."""

    __tablename__ = "document_yjs_state"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_document_yjs_state_room_document",
            ondelete="RESTRICT",
        ),
        CheckConstraint("update_counter >= 0", name="counter_non_negative"),
        CheckConstraint("byte_size = octet_length(state)", name="byte_size_matches"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    update_counter: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentSnapshot(Base):
    """A validated plain-JSON copy of the document (the editor schema's JSON). Read by search,
    asset reference tracking, restore and AI reads; never the editing authority."""

    __tablename__ = "document_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_document_snapshots_room_document",
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind IN ('seed', 'periodic', 'final', 'restore')", name="kind_valid"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="content_is_object"),
        CheckConstraint("octet_length(content_hash) = 32", name="hash_length"),
        CheckConstraint(
            "yjs_update_counter >= 0 AND text_length >= 0", name="numbers_non_negative"
        ),
        Index(
            "ix_document_snapshots_document_id_created_at", "document_id", text("created_at DESC")
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    yjs_update_counter: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    text_length: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CollabLease(Base):
    """One row. The highest fencing token may write; every acquisition increments it."""

    __tablename__ = "collab_lease"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("fencing_token >= 0", name="token_non_negative"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    holder_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
