"""ThreadForge AI tables (phase 1, checkpoint 1). Migration 0007 is the source of truth for DDL;
these mappings exist so tests and queries can use SQLAlchemy core with typed columns."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk

OPERATIONS = (
    "embed_text",
    "embed_image",
    "vision_describe",
    "vision_ocr",
    "classify",
    "map",
    "reduce",
    "answer",
    "key_validate",
    "self_check",
)


class AiGlobalFlags(Base):
    """ONE row (id = 1): the operator's global kill switch."""

    __tablename__ = "ai_global_flags"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    note: Mapped[str] = mapped_column(String(200), server_default=text("''"), nullable=False)


class AiConsent(Base):
    __tablename__ = "ai_consents"
    __table_args__ = (CheckConstraint("notice_version >= 1", name="version_positive"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    notice_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserProviderKey(Base):
    __tablename__ = "user_provider_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_provider_keys_user_id_provider"),
        CheckConstraint("provider IN ('openai', 'google')", name="provider_valid"),
        CheckConstraint("state IN ('active', 'rejected')", name="state_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    state: Mapped[str] = mapped_column(String(16), server_default=text("'active'"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AiCapabilityStatus(Base):
    __tablename__ = "ai_capability_status"

    capability: Mapped[str] = mapped_column(String(32), primary_key=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    model: Mapped[str] = mapped_column(String(120), server_default=text("''"), nullable=False)
    detail: Mapped[str] = mapped_column(String(300), server_default=text("''"), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiUsageLedger(Base):
    __tablename__ = "ai_usage_ledger"
    __table_args__ = (
        CheckConstraint("provider IN ('openai', 'google')", name="provider_valid"),
        CheckConstraint(
            "operation IN (" + ", ".join(f"'{o}'" for o in OPERATIONS) + ")", name="operation_valid"
        ),
        CheckConstraint("key_source IN ('server', 'user')", name="key_source_valid"),
        CheckConstraint("status IN ('ok', 'failed', 'cancelled')", name="status_valid"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND image_count >= 0 "
            "AND est_cost_micros >= 0",
            name="non_negative",
        ),
        CheckConstraint(
            "operation = 'self_check' OR (user_id IS NOT NULL AND charged_user_id IS NOT NULL)",
            name="user_required",
        ),
        Index("ix_ai_usage_ledger_room_created", "room_id", "created_at"),
        Index("ix_ai_usage_ledger_user_created", "charged_user_id", "created_at"),
        Index("ix_ai_usage_ledger_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    analysis_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    charged_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    room_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    image_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    est_cost_micros: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), nullable=False
    )
    key_source: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    attempt: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"), nullable=False)
    usage_estimated: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )


class AiBudgetWindow(Base):
    __tablename__ = "ai_budget_windows"
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'user', 'room')", name="scope_valid"),
        CheckConstraint("reserved_micros >= 0 AND spent_micros >= 0", name="non_negative"),
        Index("ix_ai_budget_windows_window_start", "window_start"),
    )

    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True, server_default=text("''"))
    window_start: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_micros: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), nullable=False
    )
    spent_micros: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiBudgetWarning(Base):
    __tablename__ = "ai_budget_warnings"
    __table_args__ = (CheckConstraint("threshold IN (50, 80, 100)", name="threshold_valid"),)

    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True, server_default=text("''"))
    window_start: Mapped[date] = mapped_column(Date, primary_key=True)
    threshold: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AiRateBucket(Base):
    __tablename__ = "ai_rate_buckets"
    __table_args__ = (
        CheckConstraint("count >= 0", name="count_non_negative"),
        Index("ix_ai_rate_buckets_window_start", "window_start"),
    )

    bucket: Mapped[str] = mapped_column(String(96), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
