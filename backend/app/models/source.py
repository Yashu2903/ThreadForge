"""ThreadForge AI sources (phase 1, checkpoint 2). Migration 0008 is the source of truth for DDL;
these mappings exist so tests and queries can use SQLAlchemy core with typed columns."""

from __future__ import annotations

import uuid
from datetime import datetime

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

KINDS = ("pdf", "docx", "txt", "md")
MEDIA_TYPES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
)
ANALYSIS_STATES = (
    "not_analyzed",
    "queued",
    "extracting",
    "indexing",
    "ready",
    "failed",
    "removed",
)
UPLOAD_STATES = ("intent", "uploaded", "finalized", "failed", "expired", "cancelled")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Source(Base):
    """A named file in a room. The bytes belong to its immutable VERSIONS; replacing the file adds
    a version and moves current_version_id."""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("room_id", "id", name="uq_sources_room_id_id"),
        CheckConstraint("state IN ('active', 'removed')", name="state_valid"),
        CheckConstraint(
            "(state = 'active' AND removed_at IS NULL)"
            " OR (state = 'removed' AND removed_at IS NOT NULL)",
            name="removed_consistent",
        ),
        CheckConstraint("char_length(display_name) BETWEEN 1 AND 200", name="display_name_length"),
        ForeignKeyConstraint(
            ["room_id", "current_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_sources_room_id_source_versions",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index("ix_sources_room_id_state", "room_id", "state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    state: Mapped[str] = mapped_column(String(8), server_default=text("'active'"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )


class SourceVersion(Base):
    """One immutable file. After creation only the analysis fields, error_code and the removal
    fields ever change; the bytes, hash and name never do."""

    __tablename__ = "source_versions"
    __table_args__ = (
        UniqueConstraint("source_id", "version_no", name="uq_source_versions_source_id_version_no"),
        UniqueConstraint("room_id", "id", name="uq_source_versions_room_id_id"),
        ForeignKeyConstraint(
            ["room_id", "source_id"],
            ["sources.room_id", "sources.id"],
            name="fk_source_versions_room_id_sources",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint(f"kind IN ({_in(KINDS)})", name="kind_valid"),
        CheckConstraint(f"media_type IN ({_in(MEDIA_TYPES)})", name="media_type_valid"),
        CheckConstraint(f"analysis_state IN ({_in(ANALYSIS_STATES)})", name="analysis_state_valid"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("octet_length(sha256) = 32", name="sha256_length"),
        CheckConstraint(
            "analysis_state = 'removed' OR storage_key IS NOT NULL", name="storage_key_present"
        ),
        CheckConstraint(
            "(analysis_state = 'removed') = (removed_at IS NOT NULL)", name="removed_consistent"
        ),
        CheckConstraint(
            "image_count >= 0 AND text_chars >= 0 AND (page_count IS NULL OR page_count >= 0)"
            " AND (text_page_count IS NULL OR text_page_count >= 0)"
            " AND (likely_scanned_pages IS NULL OR likely_scanned_pages >= 0)",
            name="counts_non_negative",
        ),
        ForeignKeyConstraint(
            ["room_id", "current_generation_id"],
            ["index_generations.room_id", "index_generations.id"],
            name="fk_source_versions_room_id_current_generation_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index("ix_source_versions_source_id", "source_id"),
        Index("ix_source_versions_room_id_state", "room_id", "analysis_state"),
        Index(
            "ix_source_versions_undeleted_removed",
            "removed_at",
            postgresql_where=text("analysis_state = 'removed' AND storage_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    analysis_state: Mapped[str] = mapped_column(
        String(16), server_default=text("'not_analyzed'"), nullable=False
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likely_scanned_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    text_chars: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # the ONE index generation retrieval may use; switched in a single transaction (checkpoint 3)
    current_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )


class SourceUpload(Base):
    """The intent + bytes of one device upload, until it is finalized (becomes a version), fails, is
    cancelled, or is abandoned (expires). Holds the idempotency key of the upload."""

    __tablename__ = "source_uploads"
    __table_args__ = (
        UniqueConstraint(
            "room_id",
            "created_by",
            "client_request_id",
            name="uq_source_uploads_room_id_created_by_client_request_id",
        ),
        ForeignKeyConstraint(
            ["room_id", "source_id"],
            ["sources.room_id", "sources.id"],
            name="fk_source_uploads_room_id_sources",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["room_id", "result_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_source_uploads_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"state IN ({_in(UPLOAD_STATES)})", name="state_valid"),
        CheckConstraint(f"kind IN ({_in(KINDS)})", name="kind_valid"),
        CheckConstraint("declared_size > 0", name="declared_size_positive"),
        CheckConstraint("octet_length(request_fingerprint) = 32", name="fingerprint_length"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "state <> 'uploaded' OR (storage_key IS NOT NULL AND byte_size IS NOT NULL"
            " AND sha256 IS NOT NULL)",
            name="uploaded_has_bytes",
        ),
        CheckConstraint(
            "(state = 'finalized') = (result_version_id IS NOT NULL)", name="finalized_has_version"
        ),
        Index("ix_source_uploads_state_expires_at", "state", "expires_at"),
        Index(
            "ix_source_uploads_room_id_created_by_created_at", "room_id", "created_by", "created_at"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    client_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    declared_media_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    declared_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(12), server_default=text("'intent'"), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    result_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
