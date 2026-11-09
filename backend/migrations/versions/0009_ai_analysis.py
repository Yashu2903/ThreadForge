"""ThreadForge AI phase 1, checkpoint 3: lazy analysis and retrieval

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09

EXPAND ONLY. New tables, one new nullable column, one extension; nothing existing is dropped or
rewritten:

  * CREATE EXTENSION vector    pgvector (needs a database role allowed to create extensions; the
                               Compose Postgres image is pgvector/pgvector:pg16)
  * analysis_jobs              one job per (source version, config fingerprint) that is queued or
                               running; claimed with a token and a lease by the AI worker
  * analysis_job_waiters       who is waiting for a job (a request, or an "Analyze now" click); a job
                               nobody waits for any more is cancelled
  * index_generations          one built index of a source version: building -> ready -> superseded
  * chunks                     the passages (text and figure) of a generation, with a generated
                               full-text column and an embedding (vector of any dimension; the
                               generation records which model and how many dimensions)
  * figures                    the stored image of a figure chunk
  * source_versions.current_generation_id   the ONE pointer retrieval follows; switched in a single
                               transaction when a generation is complete
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled")
STAGES = ("extracting", "ocr", "figures", "chunking", "embedding", "publishing")
GEN_STATES = ("building", "ready", "failed", "superseded")
EXTRACTIONS = ("text_layer", "tesseract", "vision_ocr", "docx", "plain", "vision")
KEY_SOURCES = ("server", "user")


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
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---- analysis_jobs --------------------------------------------------------------------------
    op.create_table(
        "analysis_jobs",
        _uuid_pk(),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("stage", sa.String(12), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anchor_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("charged_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_source_generation", sa.String(8), nullable=False),
        sa.Column("key_source_embedding", sa.String(8), nullable=False),
        sa.Column(
            "caps", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        _now("created_at"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_analysis_jobs"),
        sa.ForeignKeyConstraint(
            ["room_id", "source_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_analysis_jobs_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_analysis_jobs_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["charged_user_id"],
            ["users.id"],
            name="fk_analysis_jobs_charged_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["anchor_document_id"],
            ["documents.id"],
            name="fk_analysis_jobs_anchor_document_id_documents",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("room_id", "id", name="uq_analysis_jobs_room_id_id"),
        sa.CheckConstraint(
            f"state IN ({_in(JOB_STATES)})", name=op.f("ck_analysis_jobs_state_valid")
        ),
        sa.CheckConstraint(
            f"stage IS NULL OR stage IN ({_in(STAGES)})", name=op.f("ck_analysis_jobs_stage_valid")
        ),
        sa.CheckConstraint(
            f"key_source_generation IN ({_in(KEY_SOURCES)}) AND key_source_embedding IN ({_in(KEY_SOURCES)})",
            name=op.f("ck_analysis_jobs_key_source_valid"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1", name=op.f("ck_analysis_jobs_attempts_valid")
        ),
        sa.CheckConstraint(
            "(state = 'running') = (claim_token IS NOT NULL)",
            name=op.f("ck_analysis_jobs_claim_consistent"),
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name=op.f("ck_analysis_jobs_finished_consistent"),
        ),
    )
    # two requests that attach the same file (same configuration) share ONE active job
    op.create_index(
        "uq_analysis_jobs_active",
        "analysis_jobs",
        ["source_version_id", "config_fingerprint"],
        unique=True,
        postgresql_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index("ix_analysis_jobs_state_created_at", "analysis_jobs", ["state", "created_at"])
    op.create_index("ix_analysis_jobs_source_version_id", "analysis_jobs", ["source_version_id"])

    op.create_table(
        "analysis_job_waiters",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("waiter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("job_id", "waiter_id", name="pk_analysis_job_waiters"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["analysis_jobs.id"],
            name="fk_analysis_job_waiters_job_id_analysis_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_analysis_job_waiters_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "kind IN ('request', 'manual')", name=op.f("ck_analysis_job_waiters_kind_valid")
        ),
    )
    op.create_index("ix_analysis_job_waiters_waiter_id", "analysis_job_waiters", ["waiter_id"])

    # ---- index_generations ----------------------------------------------------------------------
    op.create_table(
        "index_generations",
        _uuid_pk(),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(10), server_default=sa.text("'building'"), nullable=False),
        sa.Column("parser_version", sa.String(24), nullable=False),
        sa.Column("chunk_policy", sa.String(24), nullable=False),
        sa.Column("embed_model", sa.String(120), nullable=False),
        sa.Column("embed_dims", sa.SmallInteger(), nullable=False),
        sa.Column("vision_model", sa.String(120), nullable=False),
        sa.Column("figure_embedding_mode", sa.String(24), nullable=False),
        sa.Column("pages_total", sa.Integer(), nullable=True),
        sa.Column("pages_unreadable", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("figure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("figures_found", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "warnings", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "gaps", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        _now("created_at"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_index_generations"),
        sa.ForeignKeyConstraint(
            ["room_id", "source_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_index_generations_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["analysis_jobs.id"],
            name="fk_index_generations_job_id_analysis_jobs",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("room_id", "id", name="uq_index_generations_room_id_id"),
        sa.CheckConstraint(
            f"state IN ({_in(GEN_STATES)})", name=op.f("ck_index_generations_state_valid")
        ),
        sa.CheckConstraint(
            "embed_dims >= 1", name=op.f("ck_index_generations_embed_dims_positive")
        ),
        sa.CheckConstraint(
            "figure_embedding_mode IN ('image', 'description', 'image_plus_description')",
            name=op.f("ck_index_generations_figure_mode_valid"),
        ),
        sa.CheckConstraint(
            "(state IN ('ready', 'superseded')) = (published_at IS NOT NULL)",
            name=op.f("ck_index_generations_published_consistent"),
        ),
        sa.CheckConstraint(
            "chunk_count >= 0 AND figure_count >= 0 AND figures_found >= 0 AND pages_unreadable >= 0",
            name=op.f("ck_index_generations_counts_non_negative"),
        ),
    )
    op.create_index(
        "ix_index_generations_source_version_id",
        "index_generations",
        ["source_version_id", "state"],
    )
    op.create_index("ix_index_generations_job_id", "index_generations", ["job_id"])

    # the pointer retrieval follows (added last: both tables exist; nullable, so expand-only)
    op.add_column(
        "source_versions",
        sa.Column("current_generation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_source_versions_room_id_current_generation_id",
        "source_versions",
        "index_generations",
        ["room_id", "current_generation_id"],
        ["room_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    # ---- chunks ---------------------------------------------------------------------------------
    op.create_table(
        "chunks",
        _uuid_pk(),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.LargeBinary(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("extraction", sa.String(12), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column(
            "flags", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("locator", postgresql.JSONB(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section_key", sa.String(300), nullable=True),
        sa.Column("embedding_dims", sa.SmallInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_chunks"),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["index_generations.id"],
            name="fk_chunks_generation_id_index_generations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "source_version_id"],
            ["source_versions.room_id", "source_versions.id"],
            name="fk_chunks_room_id_source_versions",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("generation_id", "ordinal", name="uq_chunks_generation_id_ordinal"),
        sa.CheckConstraint("kind IN ('text', 'figure')", name=op.f("ck_chunks_kind_valid")),
        sa.CheckConstraint(
            f"extraction IN ({_in(EXTRACTIONS)})", name=op.f("ck_chunks_extraction_valid")
        ),
        sa.CheckConstraint("char_length(text) >= 1", name=op.f("ck_chunks_text_not_empty")),
        sa.CheckConstraint("octet_length(text_hash) = 32", name=op.f("ck_chunks_text_hash_length")),
        sa.CheckConstraint(
            "token_count >= 1 AND ordinal >= 0", name=op.f("ck_chunks_counts_valid")
        ),
        sa.CheckConstraint(
            "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 100)",
            name=op.f("ck_chunks_ocr_confidence_range"),
        ),
    )
    # a column of pgvector type with no fixed dimension: each generation records its model and
    # dimensions, a CHECK ties every stored vector to its declared dimension, and retrieval only ever
    # looks at the pinned generations' rows (never mixing models or dimensions)
    op.execute("ALTER TABLE chunks ADD COLUMN embedding vector")
    op.execute(
        "ALTER TABLE chunks ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english'::regconfig, text)) STORED"
    )
    op.execute(
        "ALTER TABLE chunks ADD CONSTRAINT ck_chunks_embedding_dims_match CHECK "
        "(embedding IS NULL OR (embedding_dims IS NOT NULL AND vector_dims(embedding) = embedding_dims))"
    )
    op.create_index("ix_chunks_generation_id", "chunks", ["generation_id"])
    op.create_index(
        "ix_chunks_room_id_source_version_id", "chunks", ["room_id", "source_version_id"]
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")

    # ---- figures --------------------------------------------------------------------------------
    op.create_table(
        "figures",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("figure_number", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("storage_key", sa.String(128), nullable=True),
        sa.Column("media_type", sa.String(24), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_figures"),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["chunks.id"], name="fk_figures_chunk_id_chunks", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["index_generations.id"],
            name="fk_figures_generation_id_index_generations",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "generation_id", "figure_number", name="uq_figures_generation_id_figure_number"
        ),
        sa.CheckConstraint(
            "media_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name=op.f("ck_figures_media_type_valid"),
        ),
        sa.CheckConstraint("octet_length(sha256) = 32", name=op.f("ck_figures_sha256_length")),
        sa.CheckConstraint(
            "width >= 1 AND height >= 1 AND byte_size >= 1 AND figure_number >= 1",
            name=op.f("ck_figures_dimensions_valid"),
        ),
    )
    op.create_index("ix_figures_generation_id", "figures", ["generation_id"])
    op.create_index(
        "ix_figures_storage_key",
        "figures",
        ["storage_key"],
        postgresql_where=sa.text("storage_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_figures_storage_key", table_name="figures")
    op.drop_table("figures")
    op.drop_table("chunks")
    op.drop_constraint(
        "fk_source_versions_room_id_current_generation_id", "source_versions", type_="foreignkey"
    )
    op.drop_column("source_versions", "current_generation_id")
    op.drop_table("index_generations")
    op.drop_table("analysis_job_waiters")
    op.drop_table("analysis_jobs")
    # the extension is left in place on purpose: other objects may use it, and dropping an extension
    # is not something a schema downgrade should decide
