"""documents, the room's active document, image assets, document write buckets (Part 6)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08

ORDER MATTERS because rooms and documents reference each other:

  1. create documents (it references rooms, which already exists) and give it UNIQUE (room_id, id);
  2. add the NULLABLE columns rooms.active_document_id / active_document_version;
  3. add the composite foreign key rooms (id, active_document_id) -> documents (room_id, id).

Step 3 needs step 1's unique constraint to exist (a foreign key may only point at a unique key) and
step 2's columns to exist. Existing rooms get active_document_id NULL, so the new foreign key is
satisfied trivially for them: PostgreSQL's default MATCH SIMPLE skips the check whenever any
referencing column is NULL (tests/test_documents_constraints.py proves it both ways).

No room_events are fabricated for existing rooms, and no document exists until someone creates one.

The documents.import_id and room_assets.import_id columns get their foreign key to
document_imports in the migration that creates that table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
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
)
NEW_EVENT_TYPES = (
    "document_created",
    "document_renamed",
    "active_document_changed",
    "document_content_updated",
)
TEMPLATE_KEYS = (
    "blank",
    "project_brief",
    "technical_proposal",
    "meeting_notes",
    "research_summary",
    "imported",
)


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def upgrade() -> None:
    # ---- 1. documents ----------------------------------------------------------------------------
    op.create_table(
        "documents",
        _uuid_pk(),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("template_key", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("content_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("metadata_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column("import_id", postgresql.UUID(as_uuid=True), nullable=True),
        # No server defaults: assigned with clock_timestamp() after the room lock is held.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_documents_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_documents_created_by_users", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("room_id", "id", name="uq_documents_room_id_id"),
        sa.UniqueConstraint(
            "room_id", "created_by", "client_request_id", name="uq_documents_room_creator_request"
        ),
        sa.UniqueConstraint("import_id", name="uq_documents_import_id"),
        sa.CheckConstraint(
            r"title = btrim(title) AND char_length(title) BETWEEN 1 AND 200"
            r" AND title !~ '[\x01-\x1f\x7f-\x9f]'",
            name=op.f("ck_documents_title_valid"),
        ),
        sa.CheckConstraint(
            f"template_key IN ({_in_list(TEMPLATE_KEYS)})",
            name=op.f("ck_documents_template_key_valid"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_documents_schema_version_positive")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'", name=op.f("ck_documents_content_is_object")
        ),
        sa.CheckConstraint(
            "octet_length(content_hash) = 32", name=op.f("ck_documents_content_hash_length")
        ),
        sa.CheckConstraint(
            "content_version >= 1", name=op.f("ck_documents_content_version_positive")
        ),
        sa.CheckConstraint(
            "metadata_version >= 1", name=op.f("ck_documents_metadata_version_positive")
        ),
        sa.CheckConstraint(
            "(client_request_id IS NULL) = (request_fingerprint IS NULL)"
            " AND (request_fingerprint IS NULL OR octet_length(request_fingerprint) = 32)",
            name=op.f("ck_documents_request_fields_paired"),
        ),
        sa.CheckConstraint(
            "(template_key = 'imported') = (import_id IS NOT NULL)",
            name=op.f("ck_documents_import_matches_template"),
        ),
    )
    op.create_index(
        "ix_documents_room_id_created_at_id",
        "documents",
        ["room_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )

    # ---- 2. + 3. the room's active document ----
    op.add_column(
        "rooms", sa.Column("active_document_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "rooms",
        sa.Column(
            "active_document_version", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_rooms_active_document_version_non_negative"),
        "rooms",
        "active_document_version >= 0",
    )
    op.create_foreign_key(
        "fk_rooms_active_document_documents",
        "rooms",
        "documents",
        ["id", "active_document_id"],
        ["room_id", "id"],
        ondelete="RESTRICT",
    )

    # ---- image assets and who references them ----
    op.create_table(
        "room_assets",
        _uuid_pk(),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("import_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unreferenced_since", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_room_assets"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_room_assets_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name="fk_room_assets_uploaded_by_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("room_id", "id", name="uq_room_assets_room_id_id"),
        sa.CheckConstraint(
            "origin IN ('editor_upload', 'docx_import')", name=op.f("ck_room_assets_origin_valid")
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'attached', 'expired')", name=op.f("ck_room_assets_state_valid")
        ),
        sa.CheckConstraint(
            "media_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name=op.f("ck_room_assets_media_type_valid"),
        ),
        sa.CheckConstraint("byte_size > 0", name=op.f("ck_room_assets_byte_size_positive")),
        sa.CheckConstraint("octet_length(sha256) = 32", name=op.f("ck_room_assets_sha256_length")),
        sa.CheckConstraint(
            "width_px > 0 AND height_px > 0", name=op.f("ck_room_assets_dimensions_positive")
        ),
        sa.CheckConstraint(
            "char_length(storage_key) BETWEEN 1 AND 128",
            name=op.f("ck_room_assets_storage_key_length"),
        ),
        sa.CheckConstraint(
            "(origin = 'docx_import') = (import_id IS NOT NULL)",
            name=op.f("ck_room_assets_import_matches_origin"),
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND attached_at IS NULL AND unreferenced_since IS NULL)"
            " OR (state = 'attached' AND attached_at IS NOT NULL)"
            " OR state = 'expired'",
            name=op.f("ck_room_assets_state_fields_consistent"),
        ),
    )
    op.create_index("ix_room_assets_room_id_state", "room_assets", ["room_id", "state"])
    op.create_index(
        "ix_room_assets_pending_created_at",
        "room_assets",
        ["created_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_room_assets_unreferenced_since",
        "room_assets",
        ["unreferenced_since"],
        postgresql_where=sa.text("state = 'attached' AND unreferenced_since IS NOT NULL"),
    )

    op.create_table(
        "document_asset_refs",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("document_id", "asset_id", name="pk_document_asset_refs"),
        sa.ForeignKeyConstraint(
            ["room_id", "document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_document_asset_refs_room_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "asset_id"],
            ["room_assets.room_id", "room_assets.id"],
            name="fk_document_asset_refs_room_asset",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_document_asset_refs_asset_id", "document_asset_refs", ["asset_id"])

    # ---- a rate-limit table of its own, so autosave can never consume chat's quota ----
    op.create_table(
        "document_write_buckets",
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "room_id", "user_id", "window_start", name="pk_document_write_buckets"
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name="fk_document_write_buckets_room_id_rooms",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_document_write_buckets_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("count >= 0", name=op.f("ck_document_write_buckets_count_non_negative")),
    )
    op.create_index(
        "ix_document_write_buckets_window_start", "document_write_buckets", ["window_start"]
    )

    # ---- the four new room event types and the 'document' resource type ----
    op.drop_constraint(op.f("ck_room_events_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_type_valid"),
        "room_events",
        f"type IN ({_in_list(OLD_EVENT_TYPES + NEW_EVENT_TYPES)})",
    )
    op.drop_constraint(op.f("ck_room_events_resource_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_resource_type_valid"),
        "room_events",
        "resource_type IN ('room', 'membership', 'message', 'document')",
    )


def downgrade() -> None:
    # Room events are append-only and sequence numbers must stay gapless, so this migration will not
    # delete events to make a downgrade fit. If document events exist, say so instead of failing
    # later on an opaque CHECK violation.
    bind = op.get_bind()
    found = bind.execute(
        sa.text(
            "SELECT count(*) FROM room_events WHERE type IN (" + _in_list(NEW_EVENT_TYPES) + ")"  # noqa: S608
        )
    ).scalar_one()
    if found:
        raise RuntimeError(
            f"Cannot downgrade: {found} document room_events exist. Events are append-only and "
            "gapless; restore from a backup taken before documents were used instead."
        )

    op.drop_constraint(op.f("ck_room_events_resource_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_resource_type_valid"),
        "room_events",
        "resource_type IN ('room', 'membership', 'message')",
    )
    op.drop_constraint(op.f("ck_room_events_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_type_valid"), "room_events", f"type IN ({_in_list(OLD_EVENT_TYPES)})"
    )

    op.drop_index("ix_document_write_buckets_window_start", table_name="document_write_buckets")
    op.drop_table("document_write_buckets")
    op.drop_index("ix_document_asset_refs_asset_id", table_name="document_asset_refs")
    op.drop_table("document_asset_refs")
    op.drop_index("ix_room_assets_unreferenced_since", table_name="room_assets")
    op.drop_index("ix_room_assets_pending_created_at", table_name="room_assets")
    op.drop_index("ix_room_assets_room_id_state", table_name="room_assets")
    op.drop_table("room_assets")
    op.drop_constraint("fk_rooms_active_document_documents", "rooms", type_="foreignkey")
    op.drop_constraint(
        op.f("ck_rooms_active_document_version_non_negative"), "rooms", type_="check"
    )
    op.drop_column("rooms", "active_document_version")
    op.drop_column("rooms", "active_document_id")
    op.drop_index("ix_documents_room_id_created_at_id", table_name="documents")
    op.drop_table("documents")
