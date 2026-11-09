"""ThreadForge AI `@AI` request tables (phase 1, checkpoint 4). Migration 0010 is the source of truth
for DDL; these mappings let tests and queries use SQLAlchemy core with typed columns."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

REQUEST_STATES = (
    "waiting_for_sources",
    "queued",
    "running",
    "validating",
    "succeeded",
    "failed",
    "cancelling",
    "cancelled",
    "expired",
)
ACTIVE_STATES = ("waiting_for_sources", "queued", "running", "validating", "cancelling")
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "expired")
TASK_TYPES = ("qa", "summarize", "compare", "document_review")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class AiRequest(Base):
    __tablename__ = "ai_requests"
    __table_args__ = (
        UniqueConstraint(
            "room_id",
            "requested_by",
            "client_request_id",
            name="uq_ai_requests_room_requester_client",
        ),
        UniqueConstraint("message_id", name="uq_ai_requests_message_id"),
        UniqueConstraint("room_id", "id", name="uq_ai_requests_room_id_id"),
        CheckConstraint(f"state IN ({_in(REQUEST_STATES)})", name="state_valid"),
        CheckConstraint("operation = 'answer'", name="operation_valid"),
        CheckConstraint(
            "task_hint IN ('auto', 'ask', 'summarize', 'compare')", name="task_hint_valid"
        ),
        CheckConstraint(
            f"task_type IS NULL OR task_type IN ({_in(TASK_TYPES)})", name="task_type_valid"
        ),
        CheckConstraint(
            "document_scope IN ('none', 'whole', 'blocks')", name="document_scope_valid"
        ),
        CheckConstraint("char_length(instruction) BETWEEN 1 AND 4000", name="instruction_length"),
        CheckConstraint("octet_length(request_fingerprint) = 32", name="fingerprint_length"),
        CheckConstraint(
            "key_source_generation IN ('server', 'user') AND key_source_embedding IN ('server', 'user')",
            name="key_source_valid",
        ),
        CheckConstraint("attempt >= 1 AND attempt <= 10", name="attempt_valid"),
        CheckConstraint(
            "(state IN ('running', 'validating')) = (claim_token IS NOT NULL)",
            name="claim_consistent",
        ),
        CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled', 'expired')) = (finished_at IS NOT NULL)",
            name="finished_consistent",
        ),
        CheckConstraint(
            "jsonb_typeof(selected_block_ids) = 'array' AND jsonb_typeof(chat_message_ids) = 'array'",
            name="ids_are_arrays",
        ),
        Index("ix_ai_requests_state_queued_at", "state", "created_at"),
        Index("ix_ai_requests_room_id_state", "room_id", "state"),
        Index("ix_ai_requests_requested_by_state", "requested_by", "state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="RESTRICT"), nullable=False
    )
    client_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    operation: Mapped[str] = mapped_column(
        String(12), server_default=sa_text("'answer'"), nullable=False
    )
    task_hint: Mapped[str] = mapped_column(String(12), nullable=False)
    task_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    include_document_content: Mapped[bool] = mapped_column(Boolean, nullable=False)
    document_scope: Mapped[str] = mapped_column(String(8), nullable=False)
    selected_block_ids: Mapped[list] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), nullable=False
    )
    selection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    include_chat_context: Mapped[bool] = mapped_column(Boolean, nullable=False)
    chat_message_ids: Mapped[list] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), nullable=False
    )
    confirm_large: Mapped[bool] = mapped_column(
        Boolean, server_default=sa_text("false"), nullable=False
    )
    estimate: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    key_source_generation: Mapped[str] = mapped_column(String(8), nullable=False)
    key_source_embedding: Mapped[str] = mapped_column(String(8), nullable=False)
    charged_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("1"), nullable=False)
    claims: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("0"), nullable=False)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AiRequestSource(Base):
    __tablename__ = "ai_request_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "source_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_ai_request_sources_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "request_id", "position", name="uq_ai_request_sources_request_id_position"
        ),
        Index("ix_ai_request_sources_source_version_id", "source_version_id"),
        Index("ix_ai_request_sources_generation_id", "generation_id"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_requests.id", ondelete="CASCADE"), primary_key=True
    )
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_generations.id", ondelete="SET NULL"), nullable=True
    )


class AiManifest(Base):
    __tablename__ = "ai_manifests"

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_requests.id", ondelete="CASCADE"), primary_key=True
    )
    attempt: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AiResult(Base):
    __tablename__ = "ai_results"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_ai_results_request_id"),
        CheckConstraint(f"task_type IN ({_in(TASK_TYPES)})", name="task_type_valid"),
        CheckConstraint("(answer IS NULL) = (redacted_at IS NOT NULL)", name="redacted_consistent"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_requests.id", ondelete="CASCADE"), nullable=False
    )
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reply_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="RESTRICT"), nullable=True
    )
    task_type: Mapped[str] = mapped_column(String(16), nullable=False)
    answer: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    citations: Mapped[list] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), nullable=False
    )
    coverage: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    usage: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    models: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
