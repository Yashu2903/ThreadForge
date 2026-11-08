"""room events, transactional outbox, rooms.event_seq (Part 4)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08

Existing rooms start at event_seq 0 with NO fabricated events: event history begins when this
migration is deployed. A room created afterwards has room_created as seq 1.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPES = (
    "room_created",
    "room_renamed",
    "member_joined",
    "member_role_changed",
    "member_removed",
    "message_created",
    "message_edited",
    "message_deleted",
)


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("event_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(op.f("ck_rooms_event_seq_non_negative"), "rooms", "event_seq >= 0")

    type_list = ", ".join(f"'{t}'" for t in EVENT_TYPES)
    op.create_table(
        "room_events",
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resource_version", sa.Integer(), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        # No server default: assigned with clock_timestamp() inside the caller's transaction.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("room_id", "seq", name="pk_room_events"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_room_events_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_room_events_actor_id_users", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("event_id", name="uq_room_events_event_id"),
        sa.UniqueConstraint("room_id", "seq", "event_id", name="uq_room_events_room_seq_event"),
        sa.CheckConstraint("seq > 0", name=op.f("ck_room_events_seq_positive")),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_room_events_schema_version_positive")
        ),
        sa.CheckConstraint(f"type IN ({type_list})", name=op.f("ck_room_events_type_valid")),
        sa.CheckConstraint(
            "resource_type IN ('room', 'membership', 'message')",
            name=op.f("ck_room_events_resource_type_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name=op.f("ck_room_events_payload_is_object")
        ),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 2048", name=op.f("ck_room_events_payload_size")
        ),
    )

    op.create_table(
        "outbox",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(32), server_default=sa.text("'room_event'"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name="pk_outbox"),
        # ONE composite foreign key: all three columns must come from the SAME room_events row.
        sa.ForeignKeyConstraint(
            ["room_id", "room_seq", "event_id"],
            ["room_events.room_id", "room_events.seq", "room_events.event_id"],
            name="fk_outbox_room_event",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("kind IN ('room_event')", name=op.f("ck_outbox_kind_valid")),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'processed', 'failed')",
            name=op.f("ck_outbox_state_valid"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_outbox_attempts_non_negative")),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name=op.f("ck_outbox_payload_is_object")
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND claim_token IS NULL AND claimed_until IS NULL"
            " AND processed_at IS NULL)"
            " OR (state = 'processing' AND claim_token IS NOT NULL AND claimed_until IS NOT NULL"
            " AND processed_at IS NULL)"
            " OR (state = 'processed' AND processed_at IS NOT NULL AND claim_token IS NULL"
            " AND claimed_until IS NULL)"
            " OR (state = 'failed' AND claim_token IS NULL AND claimed_until IS NULL"
            " AND last_error_code IS NOT NULL)",
            name=op.f("ck_outbox_state_fields_consistent"),
        ),
    )
    op.create_index(
        "ix_outbox_pending_next_attempt_at",
        "outbox",
        ["next_attempt_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_outbox_processing_claimed_until",
        "outbox",
        ["claimed_until"],
        postgresql_where=sa.text("state = 'processing'"),
    )
    op.create_index(
        "ix_outbox_processed_processed_at",
        "outbox",
        ["processed_at"],
        postgresql_where=sa.text("state = 'processed'"),
    )
    op.create_index(
        "ix_outbox_failed_created_at",
        "outbox",
        ["created_at"],
        postgresql_where=sa.text("state = 'failed'"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_failed_created_at", table_name="outbox")
    op.drop_index("ix_outbox_processed_processed_at", table_name="outbox")
    op.drop_index("ix_outbox_processing_claimed_until", table_name="outbox")
    op.drop_index("ix_outbox_pending_next_attempt_at", table_name="outbox")
    op.drop_table("outbox")
    op.drop_table("room_events")
    op.drop_constraint(op.f("ck_rooms_event_seq_non_negative"), "rooms", type_="check")
    op.drop_column("rooms", "event_seq")
