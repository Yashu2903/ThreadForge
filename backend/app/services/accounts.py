"""Turn a verified Google identity into a local user.

Accounts are recognised ONLY by (issuer, subject). Email is never used for lookup or merging:
emails can change, be recycled, or be unverified, so matching on them would let someone take over
an account by controlling an address.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import db_error_details
from app.logging_config import APP_LOGGER
from app.models import AuthIdentity, User
from app.services.oidc import FALLBACK_DISPLAY_NAME, VerifiedIdentity

log = logging.getLogger(f"{APP_LOGGER}.accounts")

IDENTITY_UNIQUE = "uq_auth_identities_issuer_subject"
UNIQUE_VIOLATION_SQLSTATE = "23505"
DISPLAY_NAME_MAX = 100


@dataclass(frozen=True)
class ResolvedAccount:
    user_id: uuid.UUID
    state: str


def clean_display_name(name: str | None) -> str:
    """Provider name -> bounded, printable display name; fallback if nothing usable."""
    if name:
        printable = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
        collapsed = " ".join(printable.split())[:DISPLAY_NAME_MAX].strip()
        if collapsed:
            return collapsed
    return FALLBACK_DISPLAY_NAME


def is_identity_unique_violation(exc: IntegrityError) -> bool:
    """True for a unique violation on (issuer, subject): the lost side of a first-login race."""
    return db_error_details(exc) == (UNIQUE_VIOLATION_SQLSTATE, IDENTITY_UNIQUE)


async def _find_and_refresh(
    sessionmaker: async_sessionmaker[AsyncSession], identity: VerifiedIdentity
) -> ResolvedAccount | None:
    """Existing identity -> refresh the provider's email fields only; return the account."""
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(
                select(AuthIdentity, User.state)
                .join(User, User.id == AuthIdentity.user_id)
                .where(
                    AuthIdentity.issuer == identity.issuer,
                    AuthIdentity.subject == identity.subject,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        stored, state = row
        # Only the identity's provider_* fields change. users.display_name and users.email are
        # set once at creation and never overwritten by a later login.
        stored.provider_email = identity.email
        stored.provider_email_verified = identity.email_verified
        return ResolvedAccount(user_id=stored.user_id, state=state)


async def resolve_account(
    sessionmaker: async_sessionmaker[AsyncSession], identity: VerifiedIdentity
) -> ResolvedAccount:
    """Find or create the user for this identity. Safe under concurrent first logins.

    Creation is one transaction (user + identity). If a concurrent login for the same person
    commits first, our identity insert hits the unique constraint; the whole transaction,
    including our user row, is rolled back (no orphan user), and we re-select the winner's row.
    """
    existing = await _find_and_refresh(sessionmaker, identity)
    if existing is not None:
        return existing

    try:
        async with sessionmaker() as db, db.begin():
            user = User(
                display_name=clean_display_name(identity.name),
                email=identity.email,
                email_verified=identity.email_verified,
            )
            db.add(user)
            await db.flush()
            db.add(
                AuthIdentity(
                    user_id=user.id,
                    issuer=identity.issuer,
                    subject=identity.subject,
                    provider_email=identity.email,
                    provider_email_verified=identity.email_verified,
                )
            )
        return ResolvedAccount(user_id=user.id, state="active")
    except IntegrityError as exc:
        if not is_identity_unique_violation(exc):
            raise
        log.info("first-login race lost; resolving existing identity")

    winner = await _find_and_refresh(sessionmaker, identity)
    if winner is None:  # cannot happen unless the row vanished between statements
        raise RuntimeError("identity vanished after unique violation")
    return winner
