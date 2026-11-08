"""rooms, memberships, invitations (Part 2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "rooms",
        _uuid_pk(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("membership_epoch", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_rooms"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_rooms_owner_id_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 120",
            name=op.f("ck_rooms_name_valid"),
        ),
        sa.CheckConstraint("membership_epoch >= 1", name=op.f("ck_rooms_epoch_positive")),
        sa.CheckConstraint("version >= 0", name=op.f("ck_rooms_version_non_negative")),
    )
    op.create_index("ix_rooms_owner_id", "rooms", ["owner_id"])

    op.create_table(
        "memberships",
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        _ts("joined_at"),
        _ts("updated_at"),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("room_id", "user_id", name="pk_memberships"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_memberships_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_memberships_user_id_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'editor', 'viewer')", name=op.f("ck_memberships_role_valid")
        ),
        sa.CheckConstraint(
            "state IN ('active', 'removed')", name=op.f("ck_memberships_state_valid")
        ),
        sa.CheckConstraint(
            "(state = 'active' AND removed_at IS NULL)"
            " OR (state = 'removed' AND removed_at IS NOT NULL)",
            name=op.f("ck_memberships_removed_at_matches_state"),
        ),
        sa.CheckConstraint(
            "NOT (role = 'owner' AND state = 'removed')",
            name=op.f("ck_memberships_owner_not_removed"),
        ),
    )
    op.create_index("ix_memberships_user_id_state", "memberships", ["user_id", "state"])
    op.create_index(
        "uq_memberships_one_active_owner",
        "memberships",
        ["room_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner' AND state = 'active'"),
    )

    op.create_table(
        "invitations",
        _uuid_pk(),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        _ts("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_invitations"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_invitations_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_invitations_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["used_by"], ["users.id"], name="fk_invitations_used_by_users", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_invitations_token_hash_format")
        ),
        sa.CheckConstraint("role IN ('editor', 'viewer')", name=op.f("ck_invitations_role_valid")),
        sa.CheckConstraint(
            "(used_at IS NULL AND used_by IS NULL)"
            " OR (used_at IS NOT NULL AND used_by IS NOT NULL)",
            name=op.f("ck_invitations_used_pair"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_invitations_expires_after_created")
        ),
    )
    op.create_index(
        "ix_invitations_room_id_created_at_id", "invitations", ["room_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_invitations_room_id_created_at_id", table_name="invitations")
    op.drop_table("invitations")
    op.drop_index("uq_memberships_one_active_owner", table_name="memberships")
    op.drop_index("ix_memberships_user_id_state", table_name="memberships")
    op.drop_table("memberships")
    op.drop_index("ix_rooms_owner_id", table_name="rooms")
    op.drop_table("rooms")
