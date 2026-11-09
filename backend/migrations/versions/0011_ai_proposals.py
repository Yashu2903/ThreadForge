"""ThreadForge AI phase 2, checkpoint 1: proposals, applications, document seed format

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-10

EXPAND ONLY (nothing is dropped or rewritten; one CHECK constraint is widened, as 0007 and 0010 did):

  * documents.seed_format      'interchange' (default: every existing row) | 'tiptap'. How the collaboration
                               service must read `documents.content` when it seeds the first Yjs state. AI
                               drafts are stored as validated Tiptap JSON ('tiptap') because the interchange
                               format (schema_version 1, shared with the Python validator) cannot describe a diagram.
  * documents.template_key     CHECK gains 'ai_draft'
  * ai_results                 UNIQUE (request_id, id): the target of the composite key below
  * ai_proposals               one AI proposal (an edit to an existing document, or a draft of a new one),
                               its validated operations, the "before" copy of every target block, the
                               hashes it was based on, its state machine and its resolution
  * ai_proposal_applications   the durable receipt of an applied edit (UNIQUE per proposal)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TEMPLATES = (
    "blank",
    "project_brief",
    "technical_proposal",
    "meeting_notes",
    "research_summary",
    "imported",
)
NEW_TEMPLATES = (*OLD_TEMPLATES, "ai_draft")
PROPOSAL_STATES = (
    "pending",
    "applying",
    "applied",
    "rejected",
    "conflicted",
    "expired",
    "superseded",
)


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # ---- documents ------------------------------------------------------------------------------
    op.add_column(
        "documents",
        sa.Column(
            "seed_format", sa.String(16), server_default=sa.text("'interchange'"), nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_documents_seed_format_valid"),
        "documents",
        "seed_format IN ('interchange', 'tiptap')",
    )
    op.drop_constraint(op.f("ck_documents_template_key_valid"), "documents", type_="check")
    op.create_check_constraint(
        op.f("ck_documents_template_key_valid"),
        "documents",
        f"template_key IN ({_in(NEW_TEMPLATES)})",
    )

    # ---- ai_results: a composite-key target ---------------------------------------------------
    op.create_unique_constraint("uq_ai_results_request_id_id", "ai_results", ["request_id", "id"])

    # ---- ai_proposals ---------------------------------------------------------------------------
    op.create_table(
        "ai_proposals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("state", sa.String(12), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("operations", postgresql.JSONB(), nullable=False),
        sa.Column(
            "before_content",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "base_hashes", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "scope", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "preview", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("captured_counter", sa.BigInteger(), nullable=True),
        sa.Column(
            "citations", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("document_title", sa.String(200), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applying_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applying_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_code", sa.String(48), nullable=True),
        sa.Column("regenerated_from", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_proposals"),
        sa.UniqueConstraint("room_id", "id", name="uq_ai_proposals_room_id_id"),
        sa.UniqueConstraint("request_id", name="uq_ai_proposals_request_id"),
        sa.UniqueConstraint("published_document_id", name="uq_ai_proposals_published_document_id"),
        sa.ForeignKeyConstraint(
            ["room_id", "request_id"],
            ["ai_requests.room_id", "ai_requests.id"],
            name="fk_ai_proposals_room_id_ai_requests",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "result_id"],
            ["ai_results.request_id", "ai_results.id"],
            name="fk_ai_proposals_request_id_ai_results",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_ai_proposals_room_id_documents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "published_document_id"],
            ["documents.room_id", "documents.id"],
            name="fk_ai_proposals_room_id_published_documents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_ai_proposals_created_by_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["applying_by"],
            ["users.id"],
            name="fk_ai_proposals_applying_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name="fk_ai_proposals_resolved_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["regenerated_from"],
            ["ai_proposals.id"],
            name="fk_ai_proposals_regenerated_from_ai_proposals",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("kind IN ('edit', 'draft')", name=op.f("ck_ai_proposals_kind_valid")),
        sa.CheckConstraint(
            f"state IN ({_in(PROPOSAL_STATES)})", name=op.f("ck_ai_proposals_state_valid")
        ),
        sa.CheckConstraint(
            "(kind = 'edit') = (document_id IS NOT NULL)",
            name=op.f("ck_ai_proposals_target_matches_kind"),
        ),
        sa.CheckConstraint(
            "(kind = 'edit') = (captured_counter IS NOT NULL)",
            name=op.f("ck_ai_proposals_counter_matches_kind"),
        ),
        sa.CheckConstraint(
            "published_document_id IS NULL OR (kind = 'draft' AND state = 'applied')",
            name=op.f("ck_ai_proposals_published_only_when_applied_draft"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(before_content) = 'object' AND jsonb_typeof(base_hashes) = 'object'"
            " AND jsonb_typeof(scope) = 'object' AND jsonb_typeof(preview) = 'object'"
            " AND jsonb_typeof(citations) = 'array'",
            name=op.f("ck_ai_proposals_json_shapes"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(operations) IN ('array', 'object')",
            name=op.f("ck_ai_proposals_operations_shape"),
        ),
        sa.CheckConstraint(
            "(state IN ('pending', 'applying')) = (resolved_at IS NULL)",
            name=op.f("ck_ai_proposals_resolved_consistent"),
        ),
        sa.CheckConstraint(
            "(state = 'applying') = (applying_since IS NOT NULL AND applying_by IS NOT NULL)",
            name=op.f("ck_ai_proposals_applying_consistent"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_ai_proposals_expiry_after_creation")
        ),
    )
    op.create_index("ix_ai_proposals_room_id_state", "ai_proposals", ["room_id", "state"])
    op.create_index("ix_ai_proposals_document_id_state", "ai_proposals", ["document_id", "state"])
    op.create_index(
        "ix_ai_proposals_applying_since",
        "ai_proposals",
        ["applying_since"],
        postgresql_where=sa.text("state = 'applying'"),
    )
    op.create_index("ix_ai_proposals_expires_at_pending", "ai_proposals", ["expires_at"],
        postgresql_where=sa.text("state = 'pending'"))

    # ---- ai_proposal_applications ---------------------------------------------------------------
    op.create_table(
        "ai_proposal_applications",
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("applied_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resulting_counter", sa.BigInteger(), nullable=False),
        sa.Column("receipt_hash", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("proposal_id", name="pk_ai_proposal_applications"),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["ai_proposals.id"],
            name="fk_ai_proposal_applications_proposal_id_ai_proposals",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["applied_by"],
            ["users.id"],
            name="fk_ai_proposal_applications_applied_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "octet_length(receipt_hash) = 32",
            name=op.f("ck_ai_proposal_applications_receipt_hash_length"),
        ),
        sa.CheckConstraint(
            "resulting_counter >= 0", name=op.f("ck_ai_proposal_applications_counter_non_negative")
        ),
    )


def downgrade() -> None:
    op.drop_table("ai_proposal_applications")
    op.drop_table("ai_proposals")
    op.drop_constraint("uq_ai_results_request_id_id", "ai_results", type_="unique")
    op.drop_constraint(op.f("ck_documents_template_key_valid"), "documents", type_="check")
    op.create_check_constraint(
        op.f("ck_documents_template_key_valid"),
        "documents",
        f"template_key IN ({_in(OLD_TEMPLATES)})",
    )
    op.drop_constraint(op.f("ck_documents_seed_format_valid"), "documents", type_="check")
    op.drop_column("documents", "seed_format")
