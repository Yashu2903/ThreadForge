"""ThreadForge AI phase 1, checkpoint 4: `@AI` requests, manifests, results, AI chat messages

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09

EXPAND ONLY (nothing is dropped or rewritten; two CHECK constraints are widened, as migration 0007 did):

  * ai_requests          one `@AI` request: its inputs, pinned sources, state machine, lease, keys
  * ai_request_sources   the exact source VERSIONS a request pinned at admission (and, once they are
                         ready, the exact index GENERATION of each, frozen before retrieval)
  * ai_manifests         what was actually sent for one attempt of a request (immutable)
  * ai_results           the validated structured answer of a request (UNIQUE per request: one answer,
                         however many times a worker retries)
  * messages.kind        'user' | 'ai_request' | 'ai_answer'  (default 'user': every existing row)
    messages.ai_request_id   set on the two AI kinds
  * room_events          type CHECK gains ai_request_updated and source_updated; resource_type CHECK
                         gains ai_request and source
  * users                the system author "ThreadForge AI" (active, no auth identity, no sessions,
                         no membership). The application also upserts it on use, so tests that wipe
                         the table keep working.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_USER_ID = "00000000-0000-4000-8000-0000000a1a1a"
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
)
NEW_EVENT_TYPES = (*OLD_EVENT_TYPES, "ai_request_updated", "source_updated")
OLD_RESOURCE_TYPES = ("room", "membership", "message", "document")
NEW_RESOURCE_TYPES = (*OLD_RESOURCE_TYPES, "ai_request", "source")
REQUEST_STATES = (
    "waiting_for_sources",
    "queued",
    "running",
    "validating",
    "succeeded",
    "failed",
    "cancelling",
    "cancelled",
    "expired",
)
TASK_TYPES = ("qa", "summarize", "compare", "document_review")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    # ---- events ---------------------------------------------------------------------------------
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

    # ---- the system author ---------------------------------------------------------------------
    op.execute(
        f"INSERT INTO users (id, display_name, state) VALUES ('{SYSTEM_USER_ID}', 'ThreadForge AI', "
        "'active') ON CONFLICT (id) DO NOTHING"
    )

    # ---- ai_requests ---------------------------------------------------------------------------
    op.create_table(
        "ai_requests",
        _uuid_pk(),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("operation", sa.String(12), server_default=sa.text("'answer'"), nullable=False),
        sa.Column("task_hint", sa.String(12), nullable=False),
        sa.Column("task_type", sa.String(16), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("include_document_content", sa.Boolean(), nullable=False),
        sa.Column("document_scope", sa.String(8), nullable=False),
        sa.Column(
            "selected_block_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("selection_hash", sa.String(64), nullable=True),
        sa.Column("include_chat_context", sa.Boolean(), nullable=False),
        sa.Column(
            "chat_message_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("confirm_large", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "estimate", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(20), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_detail", postgresql.JSONB(), nullable=True),
        sa.Column("key_source_generation", sa.String(8), nullable=False),
        sa.Column("key_source_embedding", sa.String(8), nullable=False),
        sa.Column("charged_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("claims", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        _now("created_at"),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_requests"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_ai_requests_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_ai_requests_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["charged_user_id"],
            ["users.id"],
            name="fk_ai_requests_charged_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_ai_requests_message_id_messages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_ai_requests_document_id_documents",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "room_id",
            "requested_by",
            "client_request_id",
            name="uq_ai_requests_room_requester_client",
        ),
        sa.UniqueConstraint("message_id", name="uq_ai_requests_message_id"),
        sa.UniqueConstraint("room_id", "id", name="uq_ai_requests_room_id_id"),
        sa.CheckConstraint(
            f"state IN ({_in(REQUEST_STATES)})", name=op.f("ck_ai_requests_state_valid")
        ),
        sa.CheckConstraint("operation = 'answer'", name=op.f("ck_ai_requests_operation_valid")),
        sa.CheckConstraint(
            "task_hint IN ('auto', 'ask', 'summarize', 'compare')",
            name=op.f("ck_ai_requests_task_hint_valid"),
        ),
        sa.CheckConstraint(
            f"task_type IS NULL OR task_type IN ({_in(TASK_TYPES)})",
            name=op.f("ck_ai_requests_task_type_valid"),
        ),
        sa.CheckConstraint(
            "document_scope IN ('none', 'whole', 'blocks')",
            name=op.f("ck_ai_requests_document_scope_valid"),
        ),
        sa.CheckConstraint(
            "char_length(instruction) BETWEEN 1 AND 4000",
            name=op.f("ck_ai_requests_instruction_length"),
        ),
        sa.CheckConstraint(
            "octet_length(request_fingerprint) = 32", name=op.f("ck_ai_requests_fingerprint_length")
        ),
        sa.CheckConstraint(
            "key_source_generation IN ('server', 'user') AND key_source_embedding IN ('server', 'user')",
            name=op.f("ck_ai_requests_key_source_valid"),
        ),
        sa.CheckConstraint(
            "attempt >= 1 AND attempt <= 10", name=op.f("ck_ai_requests_attempt_valid")
        ),
        sa.CheckConstraint(
            "(state IN ('running', 'validating', 'cancelling')) = (claim_token IS NOT NULL)",
            name=op.f("ck_ai_requests_claim_consistent"),
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled', 'expired')) = (finished_at IS NOT NULL)",
            name=op.f("ck_ai_requests_finished_consistent"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(selected_block_ids) = 'array' AND jsonb_typeof(chat_message_ids) = 'array'",
            name=op.f("ck_ai_requests_ids_are_arrays"),
        ),
    )
    op.create_index("ix_ai_requests_state_queued_at", "ai_requests", ["state", "created_at"])
    op.create_index("ix_ai_requests_room_id_state", "ai_requests", ["room_id", "state"])
    op.create_index("ix_ai_requests_requested_by_state", "ai_requests", ["requested_by", "state"])

    op.create_table(
        "ai_request_sources",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("request_id", "source_version_id", name="pk_ai_request_sources"),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["ai_requests.id"],
            name="fk_ai_request_sources_request_id_ai_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "source_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_ai_request_sources_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["index_generations.id"],
            name="fk_ai_request_sources_generation_id_index_generations",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "request_id", "position", name="uq_ai_request_sources_request_id_position"
        ),
    )
    op.create_index(
        "ix_ai_request_sources_source_version_id", "ai_request_sources", ["source_version_id"]
    )
    op.create_index("ix_ai_request_sources_generation_id", "ai_request_sources", ["generation_id"])

    op.create_table(
        "ai_manifests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        _now("created_at"),
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("request_id", "attempt", name="pk_ai_manifests"),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["ai_requests.id"],
            name="fk_ai_manifests_request_id_ai_requests",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "ai_results",
        _uuid_pk(),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reply_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_type", sa.String(16), nullable=False),
        sa.Column("answer", postgresql.JSONB(), nullable=True),
        sa.Column(
            "citations", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "coverage", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "usage", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "models", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        _now("created_at"),
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ai_results"),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["ai_requests.id"],
            name="fk_ai_results_request_id_ai_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reply_message_id"],
            ["messages.id"],
            name="fk_ai_results_reply_message_id_messages",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("request_id", name="uq_ai_results_request_id"),
        sa.CheckConstraint(
            f"task_type IN ({_in(TASK_TYPES)})", name=op.f("ck_ai_results_task_type_valid")
        ),
        sa.CheckConstraint(
            "(answer IS NULL) = (redacted_at IS NOT NULL)",
            name=op.f("ck_ai_results_redacted_consistent"),
        ),
    )

    # ---- messages: kind and the request link ------------------------------------------------------
    op.add_column(
        "messages",
        sa.Column("kind", sa.String(10), server_default=sa.text("'user'"), nullable=False),
    )
    op.add_column(
        "messages", sa.Column("ai_request_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_messages_kind_valid"), "messages", "kind IN ('user', 'ai_request', 'ai_answer')"
    )
    op.create_check_constraint(
        op.f("ck_messages_ai_link_consistent"),
        "messages",
        "(kind = 'user') = (ai_request_id IS NULL)",
    )
    # the request and its message are created in one transaction, in either order
    op.create_foreign_key(
        "fk_messages_ai_request_id_ai_requests",
        "messages",
        "ai_requests",
        ["ai_request_id"],
        ["id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "ix_messages_ai_request_id",
        "messages",
        ["ai_request_id"],
        postgresql_where=sa.text("ai_request_id IS NOT NULL"),
    )
    # kind and the link never change after insert (the existing trigger guards the other columns)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION messages_forbid_immutable_updates() RETURNS trigger AS $$
        BEGIN
            IF NEW.room_id IS DISTINCT FROM OLD.room_id
               OR NEW.author_id IS DISTINCT FROM OLD.author_id
               OR NEW.client_message_id IS DISTINCT FROM OLD.client_message_id
               OR NEW.initial_request_fingerprint IS DISTINCT FROM OLD.initial_request_fingerprint
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.ai_request_id IS DISTINCT FROM OLD.ai_request_id THEN
                RAISE EXCEPTION 'messages immutable column changed'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION messages_forbid_immutable_updates() RETURNS trigger AS $$
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
    )
    op.drop_index("ix_messages_ai_request_id", table_name="messages")
    op.drop_constraint("fk_messages_ai_request_id_ai_requests", "messages", type_="foreignkey")
    op.drop_constraint(op.f("ck_messages_ai_link_consistent"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_kind_valid"), "messages", type_="check")
    op.drop_column("messages", "ai_request_id")
    op.drop_column("messages", "kind")
    op.drop_table("ai_results")
    op.drop_table("ai_manifests")
    op.drop_table("ai_request_sources")
    op.drop_table("ai_requests")
    # AI events are removed so the narrower CHECK constraints can be restored; the AI chat messages
    # themselves stay (as ordinary messages) and so does the system author who wrote the replies.
    op.execute(
        "DELETE FROM outbox WHERE event_id IN (SELECT event_id FROM room_events WHERE type IN ('ai_request_updated', 'source_updated'))"
    )
    op.execute("DELETE FROM room_events WHERE type IN ('ai_request_updated', 'source_updated')")
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
    # the system author row stays: harmless, and it may still be referenced by the replies it wrote
