"""ThreadForge AI phase 2, checkpoint 4: proposal events and regeneration

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-10

EXPAND ONLY (two CHECK constraints are widened and one nullable column is added):

  * room_events.type            also 'ai_proposal_updated'  (payload: proposal_id, state; ids and states only)
  * room_events.resource_type   also 'ai_proposal'
  * ai_requests.regenerate_of   the proposal a regeneration replaces (NULL for every ordinary request)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
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
    "room_ai_settings_changed",
    "ai_request_updated",
    "source_updated",
)
NEW_EVENT_TYPES = (*OLD_EVENT_TYPES, "ai_proposal_updated")
OLD_RESOURCE_TYPES = ("room", "membership", "message", "document", "ai_request", "source")
NEW_RESOURCE_TYPES = (*OLD_RESOURCE_TYPES, "ai_proposal")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_room_events_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_type_valid"), "room_events", f"type IN ({_in(NEW_EVENT_TYPES)})"
    )
    op.drop_constraint(op.f("ck_room_events_resource_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_resource_type_valid"),
        "room_events",
        f"resource_type IN ({_in(NEW_RESOURCE_TYPES)})",
    )
    op.add_column(
        "ai_requests", sa.Column("regenerate_of", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_ai_requests_regenerate_of_ai_proposals",
        "ai_requests",
        "ai_proposals",
        ["regenerate_of"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_ai_requests_regenerate_of_ai_proposals", "ai_requests", type_="foreignkey"
    )
    op.drop_column("ai_requests", "regenerate_of")
    # events of the new kind (and the outbox rows that point at them) go: they describe nothing the old
    # schema can represent, exactly as the downgrades of 0007 and 0010 did
    op.execute(
        "DELETE FROM outbox WHERE event_id IN (SELECT event_id FROM room_events WHERE type = 'ai_proposal_updated')"
    )
    op.execute("DELETE FROM room_events WHERE type = 'ai_proposal_updated'")
    op.drop_constraint(op.f("ck_room_events_resource_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_resource_type_valid"),
        "room_events",
        f"resource_type IN ({_in(OLD_RESOURCE_TYPES)})",
    )
    op.drop_constraint(op.f("ck_room_events_type_valid"), "room_events", type_="check")
    op.create_check_constraint(
        op.f("ck_room_events_type_valid"), "room_events", f"type IN ({_in(OLD_EVENT_TYPES)})"
    )
