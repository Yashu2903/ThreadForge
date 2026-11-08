"""messages and message_write_buckets (Part 3)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enforces "immutable after insert" in the database itself, not only by convention in the service.
IMMUTABLE_FUNCTION = """
CREATE FUNCTION messages_forbid_immutable_updates() RETURNS trigger AS $$
BEGIN
    IF NEW.room_id IS DISTINCT FROM OLD.room_id
       OR NEW.author_id IS DISTINCT FROM OLD.author_id
       OR NEW.client_message_id IS DISTINCT FROM OLD.client_message_id
       OR NEW.initial_request_fingerprint IS DISTINCT FROM OLD.initial_request_fingerprint
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'messages immutable column changed'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

IMMUTABLE_TRIGGER = """
CREATE TRIGGER trg_messages_immutable BEFORE UPDATE ON messages
FOR EACH ROW EXECUTE FUNCTION messages_forbid_immutable_updates()
"""


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("initial_request_fingerprint", sa.LargeBinary(), nullable=False),
        # No server default: assigned with clock_timestamp() under the room lock.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_messages_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_messages_author_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"], ["users.id"], name="fk_messages_deleted_by_users", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "room_id", "author_id", "client_message_id", name="uq_messages_room_author_client"
        ),
        sa.CheckConstraint(
            "(deleted_at IS NULL AND body IS NOT NULL AND char_length(body) BETWEEN 1 AND 5000)"
            " OR (deleted_at IS NOT NULL AND body IS NULL)",
            name=op.f("ck_messages_body_matches_deletion"),
        ),
        sa.CheckConstraint(
            "(deleted_at IS NULL AND deleted_by IS NULL)"
            " OR (deleted_at IS NOT NULL AND deleted_by IS NOT NULL)",
            name=op.f("ck_messages_deleted_pair"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_messages_version_positive")),
        sa.CheckConstraint(
            "octet_length(initial_request_fingerprint) = 32",
            name=op.f("ck_messages_fingerprint_length"),
        ),
    )
    op.create_index(
        "ix_messages_room_id_created_at_id", "messages", ["room_id", "created_at", "id"]
    )
    op.execute(sa.text(IMMUTABLE_FUNCTION))
    op.execute(sa.text(IMMUTABLE_TRIGGER))

    op.create_table(
        "message_write_buckets",
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "room_id", "user_id", "window_start", name="pk_message_write_buckets"
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name="fk_message_write_buckets_room_id_rooms",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_message_write_buckets_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("count >= 0", name=op.f("ck_message_write_buckets_count_non_negative")),
    )
    op.create_index(
        "ix_message_write_buckets_window_start", "message_write_buckets", ["window_start"]
    )


def downgrade() -> None:
    op.drop_index("ix_message_write_buckets_window_start", table_name="message_write_buckets")
    op.drop_table("message_write_buckets")
    op.execute(sa.text("DROP TRIGGER trg_messages_immutable ON messages"))
    op.execute(sa.text("DROP FUNCTION messages_forbid_immutable_updates()"))
    op.drop_index("ix_messages_room_id_created_at_id", table_name="messages")
    op.drop_table("messages")
