from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

TEMPLATE_KEYS = (
    "blank",
    "project_brief",
    "technical_proposal",
    "meeting_notes",
    "research_summary",
    "imported",
)
_TEMPLATE_LIST = ", ".join(f"'{k}'" for k in TEMPLATE_KEYS)


class Document(Base):
    """An editable room document. content is the ONLY editable state: the validated, canonical JSON
    tree (docs/document-schema.md). Never selected in list queries (it can be 1 MiB).

    Two independent versions: content_version (bumped by every content change; what saves are
    compared against) and metadata_version (bumped only by a rename). Timestamps have no server
    default: they are assigned with clock_timestamp() after the room lock is held.

    Rows are never deleted (no document deletion in this version). import_id gets its foreign key
    to document_imports in the migration that creates that table.
    """

    __tablename__ = "documents"
    __table_args__ = (
        # Target of the composite foreign keys that keep references inside one room.
        UniqueConstraint("room_id", "id", name="uq_documents_room_id_id"),
        UniqueConstraint(
            "room_id", "created_by", "client_request_id", name="uq_documents_room_creator_request"
        ),
        UniqueConstraint("import_id", name="uq_documents_import_id"),
        CheckConstraint(
            r"title = btrim(title) AND char_length(title) BETWEEN 1 AND 200"
            r" AND title !~ '[\x01-\x1f\x7f-\x9f]'",
            name="title_valid",
        ),
        CheckConstraint(f"template_key IN ({_TEMPLATE_LIST})", name="template_key_valid"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="content_is_object"),
        CheckConstraint("octet_length(content_hash) = 32", name="content_hash_length"),
        CheckConstraint("content_version >= 1", name="content_version_positive"),
        CheckConstraint("metadata_version >= 1", name="metadata_version_positive"),
        CheckConstraint(
            "(client_request_id IS NULL) = (request_fingerprint IS NULL)"
            " AND (request_fingerprint IS NULL OR octet_length(request_fingerprint) = 32)",
            name="request_fields_paired",
        ),
        CheckConstraint(
            "(template_key = 'imported') = (import_id IS NOT NULL)", name="import_matches_template"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    template_key: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_version: Mapped[int] = mapped_column(
        BigInteger, server_default=text("1"), nullable=False
    )
    metadata_version: Mapped[int] = mapped_column(
        BigInteger, server_default=text("1"), nullable=False
    )
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    import_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Newest-first listing: WHERE room_id = :r ORDER BY created_at DESC, id DESC
Index(
    "ix_documents_room_id_created_at_id",
    Document.room_id,
    Document.created_at.desc(),
    Document.id.desc(),
)


class RoomAsset(Base):
    """An uploaded or imported image. The bytes live in storage under an opaque key; this row is
    the metadata and the lifecycle state (pending -> attached -> expired). sha256 is of the stored,
    RE-ENCODED bytes. Processing and storage arrive with the asset pipeline."""

    __tablename__ = "room_assets"
    __table_args__ = (
        UniqueConstraint("room_id", "id", name="uq_room_assets_room_id_id"),
        CheckConstraint("origin IN ('editor_upload', 'docx_import')", name="origin_valid"),
        CheckConstraint("state IN ('pending', 'attached', 'expired')", name="state_valid"),
        CheckConstraint(
            "media_type IN ('image/png', 'image/jpeg', 'image/webp')", name="media_type_valid"
        ),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("octet_length(sha256) = 32", name="sha256_length"),
        CheckConstraint("width_px > 0 AND height_px > 0", name="dimensions_positive"),
        CheckConstraint("char_length(storage_key) BETWEEN 1 AND 128", name="storage_key_length"),
        CheckConstraint(
            "(origin = 'docx_import') = (import_id IS NOT NULL)", name="import_matches_origin"
        ),
        CheckConstraint(
            "(state = 'pending' AND attached_at IS NULL AND unreferenced_since IS NULL)"
            " OR (state = 'attached' AND attached_at IS NOT NULL)"
            " OR state = 'expired'",
            name="state_fields_consistent",
        ),
        Index("ix_room_assets_room_id_state", "room_id", "state"),
        Index(
            "ix_room_assets_pending_created_at",
            "created_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index(
            "ix_room_assets_unreferenced_since",
            "unreferenced_since",
            postgresql_where=text("state = 'attached' AND unreferenced_since IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    import_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    state: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"), nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unreferenced_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DocumentAssetRef(Base):
    """document X currently references asset Y. Maintained inside the content-save transaction.
    The composite foreign keys make a cross-room reference impossible at the database level."""

    __tablename__ = "document_asset_refs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_document_asset_refs_room_document",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["room_id", "asset_id"],
            ["room_assets.room_id", "room_assets.id"],
            name="fk_document_asset_refs_room_asset",
            ondelete="RESTRICT",
        ),
        Index("ix_document_asset_refs_asset_id", "asset_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class DocumentWriteBucket(Base):
    """Fixed-window document-save counter per (room, user, minute). A SEPARATE table from chat's,
    so autosave can never consume the chat quota (or the reverse)."""

    __tablename__ = "document_write_buckets"
    __table_args__ = (
        CheckConstraint("count >= 0", name="count_non_negative"),
        Index("ix_document_write_buckets_window_start", "window_start"),
    )

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
