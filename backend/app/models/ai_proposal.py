"""ThreadForge AI proposal tables (phase 2). Migration 0011 is the source of truth for DDL; these
mappings let tests and queries use SQLAlchemy core with typed columns."""

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
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

PROPOSAL_KINDS = ("edit", "draft")
PROPOSAL_STATES = (
    "pending",
    "applying",
    "applied",
    "rejected",
    "conflicted",
    "expired",
    "superseded",
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class AiProposal(Base):
    __tablename__ = "ai_proposals"
    __table_args__ = (
        UniqueConstraint("room_id", "id", name="uq_ai_proposals_room_id_id"),
        UniqueConstraint("request_id", name="uq_ai_proposals_request_id"),
        UniqueConstraint("published_document_id", name="uq_ai_proposals_published_document_id"),
        ForeignKeyConstraint(
            ["room_id", "request_id"],
            ["ai_requests.room_id", "ai_requests.id"],
            name="fk_ai_proposals_room_id_ai_requests",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["request_id", "result_id"],
            ["ai_results.request_id", "ai_results.id"],
            name="fk_ai_proposals_request_id_ai_results",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["room_id", "document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_ai_proposals_room_id_documents",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["room_id", "published_document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_ai_proposals_room_id_published_documents",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"kind IN ({_in(PROPOSAL_KINDS)})", name="kind_valid"),
        CheckConstraint(f"state IN ({_in(PROPOSAL_STATES)})", name="state_valid"),
        CheckConstraint("(kind = 'edit') = (document_id IS NOT NULL)", name="target_matches_kind"),
        CheckConstraint(
            "(kind = 'edit') = (captured_counter IS NOT NULL)", name="counter_matches_kind"
        ),
        CheckConstraint(
            "published_document_id IS NULL OR (kind = 'draft' AND state = 'applied')",
            name="published_only_when_applied_draft",
        ),
        CheckConstraint(
            "jsonb_typeof(before_content) = 'object' AND jsonb_typeof(base_hashes) = 'object'"
            " AND jsonb_typeof(scope) = 'object' AND jsonb_typeof(preview) = 'object'"
            " AND jsonb_typeof(citations) = 'array'",
            name="json_shapes",
        ),
        CheckConstraint("jsonb_typeof(operations) IN ('array', 'object')", name="operations_shape"),
        CheckConstraint(
            "(state IN ('pending', 'applying')) = (resolved_at IS NULL)",
            name="resolved_consistent",
        ),
        CheckConstraint(
            "(state = 'applying') = (applying_since IS NOT NULL AND applying_by IS NOT NULL)",
            name="applying_consistent",
        ),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        Index("ix_ai_proposals_room_id_state", "room_id", "state"),
        Index("ix_ai_proposals_document_id_state", "document_id", "state"),
        Index(
            "ix_ai_proposals_applying_since",
            "applying_since",
            postgresql_where=sa_text("state = 'applying'"),
        ),
        Index(
            "ix_ai_proposals_expires_at_pending",
            "expires_at",
            postgresql_where=sa_text("state = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    result_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(
        String(12), server_default=sa_text("'pending'"), nullable=False
    )
    operations: Mapped[Any] = mapped_column(JSONB, nullable=False)
    before_content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    base_hashes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    preview: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    captured_counter: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    citations: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), nullable=False
    )
    document_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applying_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    applying_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    regenerated_from: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_proposals.id", ondelete="SET NULL"), nullable=True
    )
    published_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )


class AiProposalApplication(Base):
    __tablename__ = "ai_proposal_applications"
    __table_args__ = (
        CheckConstraint("octet_length(receipt_hash) = 32", name="receipt_hash_length"),
        CheckConstraint("resulting_counter >= 0", name="counter_non_negative"),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_proposals.id", ondelete="CASCADE"), primary_key=True
    )
    applied_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resulting_counter: Mapped[int] = mapped_column(BigInteger, nullable=False)
    receipt_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
