"""ThreadForge AI analysis tables (phase 1, checkpoint 3). Migration 0009 is the source of truth for
DDL; these mappings let tests and queries use SQLAlchemy core with typed columns."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.models.base import Base, uuid_pk

JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled")
ACTIVE_JOB_STATES = ("queued", "running")
STAGES = ("extracting", "ocr", "figures", "chunking", "embedding", "publishing")
GEN_STATES = ("building", "ready", "failed", "superseded")
EXTRACTIONS = ("text_layer", "tesseract", "vision_ocr", "docx", "plain", "vision")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Vector(UserDefinedType):
    """pgvector's `vector` type with no fixed dimension. Values are written and read as text
    ('[0.1,0.2,...]') through CAST(... AS vector), so no extra Python package is needed."""

    cache_ok = True

    def get_col_spec(self, **_kw: Any) -> str:
        return "vector"


# so Alembic's reflection knows the type (keeps `alembic check` clean)
ischema_names["vector"] = Vector


def vector_literal(values: list[float]) -> str:
    """The text form pgvector parses: '[1,2,3]'. Finite floats only (checked by the caller)."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "source_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_analysis_jobs_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("room_id", "id", name="uq_analysis_jobs_room_id_id"),
        CheckConstraint(f"state IN ({_in(JOB_STATES)})", name="state_valid"),
        CheckConstraint(f"stage IS NULL OR stage IN ({_in(STAGES)})", name="stage_valid"),
        CheckConstraint(
            "key_source_generation IN ('server', 'user') AND key_source_embedding IN ('server', 'user')",
            name="key_source_valid",
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
        CheckConstraint("(state = 'running') = (claim_token IS NOT NULL)", name="claim_consistent"),
        CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="finished_consistent",
        ),
        Index(
            "uq_analysis_jobs_active",
            "source_version_id",
            "config_fingerprint",
            unique=True,
            postgresql_where=sa_text("state IN ('queued', 'running')"),
        ),
        Index("ix_analysis_jobs_state_created_at", "state", "created_at"),
        Index("ix_analysis_jobs_source_version_id", "source_version_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(10), server_default=sa_text("'queued'"), nullable=False
    )
    stage: Mapped[str | None] = mapped_column(String(12), nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    anchor_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    charged_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    key_source_generation: Mapped[str] = mapped_column(String(8), nullable=False)
    key_source_embedding: Mapped[str] = mapped_column(String(8), nullable=False)
    caps: Mapped[dict] = mapped_column(JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("0"), nullable=False)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalysisJobWaiter(Base):
    __tablename__ = "analysis_job_waiters"
    __table_args__ = (
        CheckConstraint("kind IN ('request', 'manual')", name="kind_valid"),
        Index("ix_analysis_job_waiters_waiter_id", "waiter_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    waiter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "source_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_index_generations_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("room_id", "id", name="uq_index_generations_room_id_id"),
        CheckConstraint(f"state IN ({_in(GEN_STATES)})", name="state_valid"),
        CheckConstraint("embed_dims >= 1", name="embed_dims_positive"),
        CheckConstraint(
            "figure_embedding_mode IN ('image', 'description', 'image_plus_description')",
            name="figure_mode_valid",
        ),
        CheckConstraint(
            "(state IN ('ready', 'superseded')) = (published_at IS NOT NULL)",
            name="published_consistent",
        ),
        CheckConstraint(
            "chunk_count >= 0 AND figure_count >= 0 AND figures_found >= 0 AND pages_unreadable >= 0",
            name="counts_non_negative",
        ),
        Index("ix_index_generations_source_version_id", "source_version_id", "state"),
        Index("ix_index_generations_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_jobs.id", ondelete="SET NULL"), nullable=True
    )
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(10), server_default=sa_text("'building'"), nullable=False
    )
    parser_version: Mapped[str] = mapped_column(String(24), nullable=False)
    chunk_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    embed_model: Mapped[str] = mapped_column(String(120), nullable=False)
    embed_dims: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    vision_model: Mapped[str] = mapped_column(String(120), nullable=False)
    figure_embedding_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    pages_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_unreadable: Mapped[int] = mapped_column(
        Integer, server_default=sa_text("0"), nullable=False
    )
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"), nullable=False)
    figure_count: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"), nullable=False)
    figures_found: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"), nullable=False)
    warnings: Mapped[list] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), nullable=False
    )
    gaps: Mapped[list] = mapped_column(JSONB, server_default=sa_text("'[]'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "source_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_chunks_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("generation_id", "ordinal", name="uq_chunks_generation_id_ordinal"),
        CheckConstraint("kind IN ('text', 'figure')", name="kind_valid"),
        CheckConstraint(f"extraction IN ({_in(EXTRACTIONS)})", name="extraction_valid"),
        CheckConstraint("char_length(text) >= 1", name="text_not_empty"),
        CheckConstraint("octet_length(text_hash) = 32", name="text_hash_length"),
        CheckConstraint("token_count >= 1 AND ordinal >= 0", name="counts_valid"),
        CheckConstraint(
            "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 100)",
            name="ocr_confidence_range",
        ),
        CheckConstraint(
            "embedding IS NULL OR (embedding_dims IS NOT NULL AND vector_dims(embedding) = embedding_dims)",
            name="embedding_dims_match",
        ),
        Index("ix_chunks_generation_id", "generation_id"),
        Index("ix_chunks_room_id_source_version_id", "room_id", "source_version_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    generation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=False
    )
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction: Mapped[str] = mapped_column(String(12), nullable=False)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    flags: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    embedding_dims: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=True)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, text)", persisted=True), nullable=True
    )


class Figure(Base):
    __tablename__ = "figures"
    __table_args__ = (
        UniqueConstraint(
            "generation_id", "figure_number", name="uq_figures_generation_id_figure_number"
        ),
        CheckConstraint(
            "media_type IN ('image/png', 'image/jpeg', 'image/webp')", name="media_type_valid"
        ),
        CheckConstraint("octet_length(sha256) = 32", name="sha256_length"),
        CheckConstraint(
            "width >= 1 AND height >= 1 AND byte_size >= 1 AND figure_number >= 1",
            name="dimensions_valid",
        ),
        Index("ix_figures_generation_id", "generation_id"),
        Index(
            "ix_figures_storage_key",
            "storage_key",
            postgresql_where=sa_text("storage_key IS NOT NULL"),
        ),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    generation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=False
    )
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    figure_number: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    media_type: Mapped[str] = mapped_column(String(24), nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
