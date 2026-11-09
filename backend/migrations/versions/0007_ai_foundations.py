"""ThreadForge AI phase 1, checkpoint 1: switches, consent, provider keys, spend ledger, budgets

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08

EXPAND ONLY. Nothing is dropped or rewritten:

  * rooms.ai_enabled            the owner's per-room switch (existing rooms: on)
  * ai_global_flags             ONE row: the operator's global kill switch (no redeploy needed)
  * ai_consents                 who accepted which version of the data-flow notice, and when
  * user_provider_keys          bring-your-own-key, encrypted (AES-GCM, user+provider as AAD)
  * ai_capability_status        result of the last provider self-check, per capability
  * ai_usage_ledger             one row per provider call attempt (ok, failed, cancelled)
  * ai_budget_windows           reserved + spent amounts per budget window (atomic reservation)
  * ai_budget_warnings          one row per (window, 50/80/100 percent) so each warning fires once
  * ai_rate_buckets             fixed-window counters (key-validation attempts per user / per IP)
  * room_events.type CHECK      gains room_ai_settings_changed
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_EVENT_TYPES = (
    "room_created",
    "room_renamed",
    "member_joined",
    "member_role_changed",
    "member_removed",
    "message_created",
    "message_edited",
    "message_deleted",
    "document_created",
    "document_renamed",
    "active_document_changed",
    "document_content_updated",
)
NEW_EVENT_TYPES = (*OLD_EVENT_TYPES, "room_ai_settings_changed")

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


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("ai_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )

    op.drop_constraint(op.f("ck_room_events_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_type_valid"),
        "room_events",
        f"type IN ({_in_list(NEW_EVENT_TYPES)})",
    )

    op.create_table(
        "ai_global_flags",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("ai_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("note", sa.String(200), server_default=sa.text("''"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ai_global_flags"),
        sa.CheckConstraint("id = 1", name=op.f("ck_ai_global_flags_single_row")),
    )
    op.execute("INSERT INTO ai_global_flags (id) VALUES (1)")

    op.create_table(
        "ai_consents",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notice_version", sa.Integer(), nullable=False),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("user_id", "notice_version", name="pk_ai_consents"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_ai_consents_user_id_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("notice_version >= 1", name=op.f("ck_ai_consents_version_positive")),
    )

    op.create_table(
        "user_provider_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("encrypted_key", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_user_provider_keys"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_provider_keys_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_provider_keys_user_id_provider"),
        sa.CheckConstraint(
            "provider IN ('openai', 'google')", name=op.f("ck_user_provider_keys_provider_valid")
        ),
        sa.CheckConstraint(
            "state IN ('active', 'rejected')", name=op.f("ck_user_provider_keys_state_valid")
        ),
    )

    op.create_table(
        "ai_capability_status",
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("code", sa.String(48), nullable=False),
        sa.Column("model", sa.String(120), server_default=sa.text("''"), nullable=False),
        sa.Column("detail", sa.String(300), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("capability", name="pk_ai_capability_status"),
    )

    op.create_table(
        "ai_usage_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # request / analysis job ids: foreign keys are added with those tables in later checkpoints
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("analysis_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        # NULL only for the operator/worker self-check (no user is involved in those calls)
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("charged_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("operation", sa.String(24), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("image_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("est_cost_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("key_source", sa.String(8), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("attempt", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("usage_estimated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ai_usage_ledger"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_ai_usage_ledger_user_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["charged_user_id"],
            ["users.id"],
            name="fk_ai_usage_ledger_charged_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_ai_usage_ledger_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "provider IN ('openai', 'google')", name=op.f("ck_ai_usage_ledger_provider_valid")
        ),
        sa.CheckConstraint(
            f"operation IN ({_in_list(OPERATIONS)})",
            name=op.f("ck_ai_usage_ledger_operation_valid"),
        ),
        sa.CheckConstraint(
            "key_source IN ('server', 'user')", name=op.f("ck_ai_usage_ledger_key_source_valid")
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'failed', 'cancelled')", name=op.f("ck_ai_usage_ledger_status_valid")
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND image_count >= 0 AND est_cost_micros >= 0",
            name=op.f("ck_ai_usage_ledger_non_negative"),
        ),
        sa.CheckConstraint(
            "operation = 'self_check' OR (user_id IS NOT NULL AND charged_user_id IS NOT NULL)",
            name=op.f("ck_ai_usage_ledger_user_required"),
        ),
    )
    op.create_index("ix_ai_usage_ledger_room_created", "ai_usage_ledger", ["room_id", "created_at"])
    op.create_index(
        "ix_ai_usage_ledger_user_created", "ai_usage_ledger", ["charged_user_id", "created_at"]
    )
    op.create_index("ix_ai_usage_ledger_created", "ai_usage_ledger", ["created_at"])

    op.create_table(
        "ai_budget_windows",
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("scope_key", sa.String(64), server_default=sa.text("''"), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("reserved_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("spent_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("scope", "scope_key", "window_start", name="pk_ai_budget_windows"),
        sa.CheckConstraint(
            "scope IN ('global', 'user', 'room')", name=op.f("ck_ai_budget_windows_scope_valid")
        ),
        sa.CheckConstraint(
            "reserved_micros >= 0 AND spent_micros >= 0",
            name=op.f("ck_ai_budget_windows_non_negative"),
        ),
    )
    op.create_index("ix_ai_budget_windows_window_start", "ai_budget_windows", ["window_start"])

    op.create_table(
        "ai_budget_warnings",
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("scope_key", sa.String(64), server_default=sa.text("''"), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("threshold", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "scope", "scope_key", "window_start", "threshold", name="pk_ai_budget_warnings"
        ),
        sa.CheckConstraint(
            "threshold IN (50, 80, 100)", name=op.f("ck_ai_budget_warnings_threshold_valid")
        ),
    )

    op.create_table(
        "ai_rate_buckets",
        sa.Column("bucket", sa.String(96), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("bucket", "window_start", name="pk_ai_rate_buckets"),
        sa.CheckConstraint("count >= 0", name=op.f("ck_ai_rate_buckets_count_non_negative")),
    )
    op.create_index("ix_ai_rate_buckets_window_start", "ai_rate_buckets", ["window_start"])


def downgrade() -> None:
    op.drop_table("ai_rate_buckets")
    op.drop_table("ai_budget_warnings")
    op.drop_table("ai_budget_windows")
    op.drop_table("ai_usage_ledger")
    op.drop_table("ai_capability_status")
    op.drop_table("user_provider_keys")
    op.drop_table("ai_consents")
    op.drop_table("ai_global_flags")
    # a room_ai_settings_changed event would violate the old CHECK: remove those events (and the
    # outbox rows that reference them) first. They are change notifications, not records.
    op.execute(
        "DELETE FROM outbox WHERE event_id IN "
        "(SELECT event_id FROM room_events WHERE type = 'room_ai_settings_changed')"
    )
    op.execute("DELETE FROM room_events WHERE type = 'room_ai_settings_changed'")
    op.drop_constraint(op.f("ck_room_events_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_type_valid"),
        "room_events",
        f"type IN ({_in_list(OLD_EVENT_TYPES)})",
    )
    op.drop_column("rooms", "ai_enabled")
