from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now_column, uuid_pk


class OIDCLoginAttempt(Base):
    """One in-flight Google login, stored server-side so any API process can finish it.

    state and the browser-binding secret are stored only as hashes. nonce and pkce_verifier are
    stored raw because they must be replayed to Google or compared with the ID token. They are
    useless without the authorization code and the matching browser cookie, and they live at
    most ten minutes.
    """

    __tablename__ = "oidc_login_attempts"
    __table_args__ = (
        CheckConstraint("state_hash ~ '^[0-9a-f]{64}$'", name="state_hash_format"),
        CheckConstraint("browser_binding_hash ~ '^[0-9a-f]{64}$'", name="binding_hash_format"),
        CheckConstraint("char_length(nonce) BETWEEN 16 AND 255", name="nonce_length"),
        CheckConstraint(
            "char_length(pkce_verifier) BETWEEN 43 AND 128", name="pkce_verifier_length"
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumed_after_created"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    browser_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(255), nullable=False)
    pkce_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = utc_now_column()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
