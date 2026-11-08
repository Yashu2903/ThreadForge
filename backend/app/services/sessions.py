"""ThreadForge browser sessions.

OIDC proves who the person is once, at login. After that, *our* session (a random secret in an
HttpOnly cookie) is what keeps them logged in. The browser holds the secret; PostgreSQL holds only
its SHA-256, so a database leak or backup does not yield usable cookies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Session, User
from app.services.security import is_well_formed_secret, new_secret, sha256_hex

_SESSIONS = Session.__table__


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    display_name: str
    email: str | None
    email_verified: bool
    state: str


@dataclass(frozen=True)
class AuthContext:
    """A validated session and its user. Contains the raw session secret (needed to derive the
    CSRF token), so repr masks it."""

    user: AuthUser
    session_id: uuid.UUID
    session_secret: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"AuthContext(user_id={self.user.id}, session_id={self.session_id}, "
            "session_secret='***')"
        )


@dataclass(frozen=True)
class NewSession:
    id: uuid.UUID
    secret: str = field(repr=False)
    expires_at: datetime


async def create_session(db: AsyncSession, settings: Settings, user_id: uuid.UUID) -> NewSession:
    """Create a fresh session with a new random secret. Caller owns the transaction.

    Expiry is computed by the database clock, the same clock every later expiry check uses.
    Absolute lifetime only: no sliding expiry and no last-seen writes in this part.
    """
    secret = new_secret()
    row = (
        await db.execute(
            insert(_SESSIONS)
            .values(
                user_id=user_id,
                secret_hash=sha256_hex(secret),
                expires_at=func.now() + timedelta(seconds=settings.session_lifetime_seconds),
            )
            .returning(_SESSIONS.c.id, _SESSIONS.c.expires_at)
        )
    ).one()
    return NewSession(id=row.id, secret=secret, expires_at=row.expires_at)


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> bool:
    """Mark a session revoked (idempotent). Returns True if this call revoked it."""
    result = await db.execute(
        update(_SESSIONS)
        .where(_SESSIONS.c.id == session_id, _SESSIONS.c.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    return result.rowcount == 1


async def load_session(db: AsyncSession, secret: str | None) -> AuthContext | None:
    """Look up a session by cookie secret. None unless it exists, is unexpired and unrevoked.

    The user's state is returned, not enforced here (logout must still work for a disabled
    user's session); get_current_user enforces `active`.
    """
    if not is_well_formed_secret(secret):
        return None
    assert secret is not None
    row = (
        await db.execute(
            select(
                _SESSIONS.c.id,
                User.id.label("user_id"),
                User.display_name,
                User.email,
                User.email_verified,
                User.state,
            )
            .join(User, User.id == _SESSIONS.c.user_id)
            .where(
                _SESSIONS.c.secret_hash == sha256_hex(secret),
                _SESSIONS.c.revoked_at.is_(None),
                _SESSIONS.c.expires_at > func.now(),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return AuthContext(
        user=AuthUser(
            id=row.user_id,
            display_name=row.display_name,
            email=row.email,
            email_verified=row.email_verified,
            state=row.state,
        ),
        session_id=row.id,
        session_secret=secret,
    )
