"""auth foundation: users, auth_identities, sessions, oidc_login_attempts

Revision ID: 0001
Revises:
Create Date: 2026-11-07

Hand-written so every constraint and default is explicit and reviewable. `alembic check` (run in
the test suite) proves the models and this migration have not drifted apart.

gen_random_uuid() is built into PostgreSQL 13+, so no extension is required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.CheckConstraint(
            "char_length(btrim(display_name)) BETWEEN 1 AND 100",
            name=op.f("ck_users_display_name_length"),
        ),
        sa.CheckConstraint(
            "email IS NULL OR char_length(email) BETWEEN 1 AND 320",
            name=op.f("ck_users_email_length"),
        ),
        sa.CheckConstraint(
            "state IN ('active', 'disabled', 'deleted')", name=op.f("ck_users_state_valid")
        ),
        sa.CheckConstraint("version >= 0", name=op.f("ck_users_version_non_negative")),
    )

    op.create_table(
        "auth_identities",
        _uuid_pk(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("provider_email", sa.String(320), nullable=True),
        sa.Column(
            "provider_email_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        _created_at(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_identities"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_auth_identities_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("issuer", "subject", name="uq_auth_identities_issuer_subject"),
        sa.CheckConstraint(
            "issuer LIKE 'https://%' AND char_length(issuer) <= 255",
            name=op.f("ck_auth_identities_issuer_valid"),
        ),
        sa.CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 255", name=op.f("ck_auth_identities_subject_length")
        ),
        sa.CheckConstraint(
            "provider_email IS NULL OR char_length(provider_email) BETWEEN 1 AND 320",
            name=op.f("ck_auth_identities_provider_email_length"),
        ),
    )
    op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])

    op.create_table(
        "sessions",
        _uuid_pk(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("secret_hash", name="uq_sessions_secret_hash"),
        sa.CheckConstraint(
            "secret_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_sessions_secret_hash_format")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_sessions_expires_after_created")
        ),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    op.create_table(
        "oidc_login_attempts",
        _uuid_pk(),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("browser_binding_hash", sa.String(64), nullable=False),
        sa.Column("nonce", sa.String(255), nullable=False),
        sa.Column("pkce_verifier", sa.String(128), nullable=False),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_oidc_login_attempts"),
        sa.UniqueConstraint("state_hash", name="uq_oidc_login_attempts_state_hash"),
        sa.CheckConstraint(
            "state_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_oidc_login_attempts_state_hash_format")
        ),
        sa.CheckConstraint(
            "browser_binding_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_oidc_login_attempts_binding_hash_format"),
        ),
        sa.CheckConstraint(
            "char_length(nonce) BETWEEN 16 AND 255",
            name=op.f("ck_oidc_login_attempts_nonce_length"),
        ),
        sa.CheckConstraint(
            "char_length(pkce_verifier) BETWEEN 43 AND 128",
            name=op.f("ck_oidc_login_attempts_pkce_verifier_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_oidc_login_attempts_expires_after_created")
        ),
    )
    op.create_index("ix_oidc_login_attempts_expires_at", "oidc_login_attempts", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_oidc_login_attempts_expires_at", table_name="oidc_login_attempts")
    op.drop_table("oidc_login_attempts")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_auth_identities_user_id", table_name="auth_identities")
    op.drop_table("auth_identities")
    op.drop_table("users")
