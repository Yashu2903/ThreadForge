"""collaborative documents (Yjs): collab_state, Yjs state, snapshots, collaboration lease

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08

EXPAND ONLY. Nothing is dropped or rewritten:

  * documents.collab_state    legacy | ready | needs_repair   (every existing row becomes 'legacy')
  * document_yjs_state        the ONLY writable content of a 'ready' document (Yjs binary)
  * document_snapshots        validated plain-JSON copies of the Yjs document (search, assets, AI reads)
  * collab_lease              ONE row: which collaboration-service instance may write, with a
                              fencing token that increases on every acquisition

The old columns and tables (documents.content / content_version / content_hash,
document_write_buckets, the 'document_content_updated' event type) stay; documents.content is now
read-only SEED material (docs/collaboration.md). They are listed as deprecated in the README.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("collab_state", sa.String(16), server_default=sa.text("'legacy'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_documents_collab_state_valid"),
        "documents",
        "collab_state IN ('legacy', 'ready', 'needs_repair')",
    )

    op.create_table(
        "document_yjs_state",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=False),
        sa.Column("update_counter", sa.BigInteger(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("document_id", name="pk_document_yjs_state"),
        sa.ForeignKeyConstraint(
            ["room_id", "document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_document_yjs_state_room_document",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "update_counter >= 0", name=op.f("ck_document_yjs_state_counter_non_negative")
        ),
        sa.CheckConstraint(
            "byte_size = octet_length(state)", name=op.f("ck_document_yjs_state_byte_size_matches")
        ),
    )

    op.create_table(
        "document_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("yjs_update_counter", sa.BigInteger(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("text_length", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document_snapshots"),
        sa.ForeignKeyConstraint(
            ["room_id", "document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_document_snapshots_room_document",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "kind IN ('seed', 'periodic', 'final', 'restore')",
            name=op.f("ck_document_snapshots_kind_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'", name=op.f("ck_document_snapshots_content_is_object")
        ),
        sa.CheckConstraint(
            "octet_length(content_hash) = 32", name=op.f("ck_document_snapshots_hash_length")
        ),
        sa.CheckConstraint(
            "yjs_update_counter >= 0 AND text_length >= 0",
            name=op.f("ck_document_snapshots_numbers_non_negative"),
        ),
    )
    op.create_index(
        "ix_document_snapshots_document_id_created_at",
        "document_snapshots",
        ["document_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "collab_lease",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("holder_id", sa.String(128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_collab_lease"),
        sa.CheckConstraint("id = 1", name=op.f("ck_collab_lease_single_row")),
        sa.CheckConstraint("fencing_token >= 0", name=op.f("ck_collab_lease_token_non_negative")),
    )


def downgrade() -> None:
    # Dropping the Yjs state would destroy the only copy of every 'ready' document's content.
    bind = op.get_bind()
    found = bind.execute(sa.text("SELECT count(*) FROM document_yjs_state")).scalar_one()
    if found:
        raise RuntimeError(
            f"Cannot downgrade: {found} document(s) keep their content only in document_yjs_state. "
            "Restore from a backup taken before collaboration was used instead."
        )
    op.drop_table("collab_lease")
    op.drop_index("ix_document_snapshots_document_id_created_at", table_name="document_snapshots")
    op.drop_table("document_snapshots")
    op.drop_table("document_yjs_state")
    op.drop_constraint(op.f("ck_documents_collab_state_valid"), "documents", type_="check")
    op.drop_column("documents", "collab_state")
