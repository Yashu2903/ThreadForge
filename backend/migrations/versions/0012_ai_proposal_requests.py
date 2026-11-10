"""ThreadForge AI phase 2, checkpoint 2: edit and draft requests

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-10

EXPAND ONLY (three CHECK constraints are widened and one NOT NULL is relaxed, as 0007, 0010 and 0011 did):

  * ai_requests.operation      'answer' | 'edit' | 'draft'
  * ai_requests.document_id    nullable: a DRAFT is anchored on the room role, not on a document, so a room with
                               no documents can start with one (an answer and an edit still need a document)
  * ai_requests.document_scope also 'edit' (an explicit target: blocks, ranges, or an insertion point)
  * ai_requests.selected_ranges  character ranges [(block_id, start, end, text_hash)] of an edit target
  * ai_requests.insert_at      'start' | 'end' | NULL: where an insertion with no selection goes
  * ai_requests.task_type, ai_results.task_type   also 'edit_proposal' and 'draft'
  * analysis_jobs.anchor_document_id   nullable, for the same reason: the analysis a draft waits for has no
                               document to be anchored on (its worker re-checks the role and consent instead)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TASKS = ("qa", "summarize", "compare", "document_review")
NEW_TASKS = (*OLD_TASKS, "edit_proposal", "draft")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ai_requests_operation_valid"), "ai_requests", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_requests_operation_valid"),
        "ai_requests",
        "operation IN ('answer', 'edit', 'draft')",
    )
    op.drop_constraint(op.f("ck_ai_requests_document_scope_valid"), "ai_requests", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_requests_document_scope_valid"),
        "ai_requests",
        "document_scope IN ('none', 'whole', 'blocks', 'edit')",
    )
    op.drop_constraint(op.f("ck_ai_requests_task_type_valid"), "ai_requests", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_requests_task_type_valid"),
        "ai_requests",
        f"task_type IS NULL OR task_type IN ({_in(NEW_TASKS)})",
    )
    op.drop_constraint(op.f("ck_ai_results_task_type_valid"), "ai_results", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_results_task_type_valid"), "ai_results", f"task_type IN ({_in(NEW_TASKS)})"
    )

    op.alter_column("ai_requests", "document_id", existing_type=postgresql.UUID(), nullable=True)
    op.alter_column(
        "analysis_jobs", "anchor_document_id", existing_type=postgresql.UUID(), nullable=True
    )
    op.add_column(
        "ai_requests",
        sa.Column(
            "selected_ranges",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("ai_requests", sa.Column("insert_at", sa.String(5), nullable=True))
    op.create_check_constraint(
        op.f("ck_ai_requests_document_required_unless_draft"),
        "ai_requests",
        "operation = 'draft' OR document_id IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_ai_requests_edit_target_shape"),
        "ai_requests",
        "(operation = 'edit') = (document_scope = 'edit')"
        " AND jsonb_typeof(selected_ranges) = 'array'"
        " AND (insert_at IS NULL OR (operation = 'edit' AND insert_at IN ('start', 'end')))",
    )


def downgrade() -> None:
    # Rows that only exist because of this migration cannot be represented by the old constraints, and they
    # are tied to chat messages and proposals, so the downgrade refuses instead of deleting someone's history.
    leftover = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM ai_requests WHERE operation <> 'answer'"))
        .scalar_one()
    )
    if leftover:
        raise RuntimeError(
            f"{leftover} edit/draft request(s) exist; this downgrade will not delete AI history. "
            "Remove them deliberately first if you really want to go back."
        )
    op.drop_constraint(op.f("ck_ai_requests_edit_target_shape"), "ai_requests", type_="check")
    op.drop_constraint(
        op.f("ck_ai_requests_document_required_unless_draft"), "ai_requests", type_="check"
    )
    unanchored = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM analysis_jobs WHERE anchor_document_id IS NULL"))
        .scalar_one()
    )
    if unanchored:
        raise RuntimeError(
            f"{unanchored} analysis job(s) have no anchor document (they were queued for a draft); "
            "wait for them to finish and clean up before going back."
        )
    op.alter_column(
        "analysis_jobs", "anchor_document_id", existing_type=postgresql.UUID(), nullable=False
    )
    op.drop_column("ai_requests", "insert_at")
    op.drop_column("ai_requests", "selected_ranges")
    op.alter_column("ai_requests", "document_id", existing_type=postgresql.UUID(), nullable=False)
    op.drop_constraint(op.f("ck_ai_results_task_type_valid"), "ai_results", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_results_task_type_valid"), "ai_results", f"task_type IN ({_in(OLD_TASKS)})"
    )
    op.drop_constraint(op.f("ck_ai_requests_task_type_valid"), "ai_requests", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_requests_task_type_valid"),
        "ai_requests",
        f"task_type IS NULL OR task_type IN ({_in(OLD_TASKS)})",
    )
    op.drop_constraint(op.f("ck_ai_requests_document_scope_valid"), "ai_requests", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_requests_document_scope_valid"),
        "ai_requests",
        "document_scope IN ('none', 'whole', 'blocks')",
    )
    op.drop_constraint(op.f("ck_ai_requests_operation_valid"), "ai_requests", type_="check")
    op.create_check_constraint(
        op.f("ck_ai_requests_operation_valid"), "ai_requests", "operation = 'answer'"
    )
