"""ThreadForge AI phase 1, checkpoint 2: sources (files attached to AI requests)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09

EXPAND ONLY. Three new tables, nothing existing is touched:

  * sources          a named file in a room; points at its current version
  * source_versions  one immutable file (bytes under an opaque storage key, hash, inspection counts,
                     analysis state). Replacing a file adds a version.
  * source_uploads   the intent + bytes of one device upload until it is finalized, fails, is
                     cancelled or is abandoned; carries the idempotency key

sources.current_version_id points at source_versions and source_versions.source_id points at
sources, so the pointer's foreign key is added AFTER both tables exist and is DEFERRABLE (a source
and its first version are inserted in one transaction).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("pdf", "docx", "txt", "md")
MEDIA_TYPES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
)
ANALYSIS_STATES = (
    "not_analyzed",
    "queued",
    "extracting",
    "indexing",
    "ready",
    "failed",
    "removed",
)
UPLOAD_STATES = ("intent", "uploaded", "finalized", "failed", "expired", "cancelled")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
    )


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "sources",
        _uuid_pk(),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(8), server_default=sa.text("'active'"), nullable=False),
        _now("created_at"),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_sources_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_sources_created_by_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["removed_by"], ["users.id"], name="fk_sources_removed_by_users", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("room_id", "id", name="uq_sources_room_id_id"),
        sa.CheckConstraint("state IN ('active', 'removed')", name=op.f("ck_sources_state_valid")),
        sa.CheckConstraint(
            "(state = 'active' AND removed_at IS NULL)"
            " OR (state = 'removed' AND removed_at IS NOT NULL)",
            name=op.f("ck_sources_removed_consistent"),
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 200",
            name=op.f("ck_sources_display_name_length"),
        ),
    )
    op.create_index("ix_sources_room_id_state", "sources", ["room_id", "state"])

    op.create_table(
        "source_versions",
        _uuid_pk(),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("storage_key", sa.String(128), nullable=True),
        sa.Column(
            "analysis_state", sa.String(16), server_default=sa.text("'not_analyzed'"), nullable=False
        ),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("text_page_count", sa.Integer(), nullable=True),
        sa.Column("likely_scanned_pages", sa.Integer(), nullable=True),
        sa.Column("image_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("text_chars", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        _now("created_at"),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_source_versions"),
        sa.ForeignKeyConstraint(
            ["room_id", "source_id"],
            ["sources.room_id", "sources.id"],
            name="fk_source_versions_room_id_sources",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_source_versions_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_id", "version_no", name="uq_source_versions_source_id_version_no"
        ),
        sa.UniqueConstraint("room_id", "id", name="uq_source_versions_room_id_id"),
        sa.CheckConstraint("version_no >= 1", name=op.f("ck_source_versions_version_no_positive")),
        sa.CheckConstraint(f"kind IN ({_in(KINDS)})", name=op.f("ck_source_versions_kind_valid")),
        sa.CheckConstraint(
            f"media_type IN ({_in(MEDIA_TYPES)})", name=op.f("ck_source_versions_media_type_valid")
        ),
        sa.CheckConstraint(
            f"analysis_state IN ({_in(ANALYSIS_STATES)})",
            name=op.f("ck_source_versions_analysis_state_valid"),
        ),
        sa.CheckConstraint("byte_size > 0", name=op.f("ck_source_versions_byte_size_positive")),
        sa.CheckConstraint(
            "octet_length(sha256) = 32", name=op.f("ck_source_versions_sha256_length")
        ),
        sa.CheckConstraint(
            "analysis_state = 'removed' OR storage_key IS NOT NULL",
            name=op.f("ck_source_versions_storage_key_present"),
        ),
        sa.CheckConstraint(
            "(analysis_state = 'removed') = (removed_at IS NOT NULL)",
            name=op.f("ck_source_versions_removed_consistent"),
        ),
        sa.CheckConstraint(
            "image_count >= 0 AND text_chars >= 0 AND (page_count IS NULL OR page_count >= 0)"
            " AND (text_page_count IS NULL OR text_page_count >= 0)"
            " AND (likely_scanned_pages IS NULL OR likely_scanned_pages >= 0)",
            name=op.f("ck_source_versions_counts_non_negative"),
        ),
    )
    op.create_index("ix_source_versions_source_id", "source_versions", ["source_id"])
    op.create_index(
        "ix_source_versions_room_id_state", "source_versions", ["room_id", "analysis_state"]
    )
    op.create_index(
        "ix_source_versions_undeleted_removed",
        "source_versions",
        ["removed_at"],
        postgresql_where=sa.text("analysis_state = 'removed' AND storage_key IS NOT NULL"),
    )

    # The pointer is added last: both tables exist, and it is checked at COMMIT so a source and its
    # first version can be inserted in the same transaction.
    op.create_foreign_key(
        "fk_sources_room_id_source_versions",
        "sources",
        "source_versions",
        ["room_id", "current_version_id"],
        ["room_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "source_uploads",
        _uuid_pk(),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("declared_media_type", sa.String(100), nullable=True),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(12), server_default=sa.text("'intent'"), nullable=False),
        sa.Column("storage_key", sa.String(128), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.LargeBinary(), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("result_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        _now("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_source_uploads"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_source_uploads_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_source_uploads_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "source_id"],
            ["sources.room_id", "sources.id"],
            name="fk_source_uploads_room_id_sources",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "result_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_source_uploads_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "room_id",
            "created_by",
            "client_request_id",
            name="uq_source_uploads_room_id_created_by_client_request_id",
        ),
        sa.CheckConstraint(f"state IN ({_in(UPLOAD_STATES)})", name=op.f("ck_source_uploads_state_valid")),
        sa.CheckConstraint(f"kind IN ({_in(KINDS)})", name=op.f("ck_source_uploads_kind_valid")),
        sa.CheckConstraint(
            "declared_size > 0", name=op.f("ck_source_uploads_declared_size_positive")
        ),
        sa.CheckConstraint(
            "octet_length(request_fingerprint) = 32",
            name=op.f("ck_source_uploads_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_source_uploads_expiry_after_creation")
        ),
        sa.CheckConstraint(
            "state <> 'uploaded' OR (storage_key IS NOT NULL AND byte_size IS NOT NULL"
            " AND sha256 IS NOT NULL)",
            name=op.f("ck_source_uploads_uploaded_has_bytes"),
        ),
        sa.CheckConstraint(
            "(state = 'finalized') = (result_version_id IS NOT NULL)",
            name=op.f("ck_source_uploads_finalized_has_version"),
        ),
    )
    op.create_index(
        "ix_source_uploads_state_expires_at", "source_uploads", ["state", "expires_at"]
    )
    op.create_index(
        "ix_source_uploads_room_id_created_by_created_at",
        "source_uploads",
        ["room_id", "created_by", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("source_uploads")
    op.drop_constraint("fk_sources_room_id_source_versions", "sources", type_="foreignkey")
    op.drop_table("source_versions")
    op.drop_table("sources")
