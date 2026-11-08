"""Server-side OIDC login attempts.

Why a table instead of a signed cookie: the callback may land on a different API process, and a
row can be consumed exactly once with a single atomic statement.

Why a separate browser-binding secret: `state` travels in URLs (authorization URL, callback URL,
browser history, proxy logs). Anyone who obtains a valid callback URL could otherwise finish the
login in *their* browser and land in the victim's flow (login CSRF / session fixation). The
binding secret never leaves the originating browser's HttpOnly cookie, so the callback only
succeeds in the browser that started the login.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import APP_LOGGER
from app.models import OIDCLoginAttempt
from app.services.security import new_secret, sha256_hex

log = logging.getLogger(f"{APP_LOGGER}.login_attempts")

_ATTEMPTS = OIDCLoginAttempt.__table__
# Serialises cap-check + insert so the cap is exact under concurrency.
_LOCK_KEY = 0x7F_00_01
# Bound the work done by the opportunistic cleanup on each login request.
_CLEANUP_BATCH = 500


@dataclass(frozen=True)
class LoginSecrets:
    """The four random values for one login. Never log or repr these."""

    state: str = field(default_factory=new_secret, repr=False)
    binding_secret: str = field(default_factory=new_secret, repr=False)
    nonce: str = field(default_factory=new_secret, repr=False)
    # 64 random bytes -> 86 characters, inside RFC 7636's 43-128 range.
    code_verifier: str = field(default_factory=lambda: new_secret(64), repr=False)


@dataclass(frozen=True)
class ConsumedAttempt:
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)


class LoginBusy(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("login_busy")
        self.retry_after = retry_after


async def store_attempt(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, secrets_: LoginSecrets
) -> None:
    """Insert the attempt (after cleanup and the cap check) and commit.

    Raises LoginBusy when too many unexpired attempts are pending. One transaction, no network.
    """
    busy_retry_after: int | None = None
    async with sessionmaker() as db, db.begin():
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})

        expired = select(_ATTEMPTS.c.id).where(_ATTEMPTS.c.expires_at <= func.now())
        await db.execute(delete(_ATTEMPTS).where(_ATTEMPTS.c.id.in_(expired.limit(_CLEANUP_BATCH))))

        pending, soonest = (
            await db.execute(
                select(func.count(), func.min(_ATTEMPTS.c.expires_at)).where(
                    _ATTEMPTS.c.consumed_at.is_(None), _ATTEMPTS.c.expires_at > func.now()
                )
            )
        ).one()

        if pending >= settings.max_pending_login_attempts:
            until_free = (soonest - datetime.now(UTC)).total_seconds() if soonest else 1
            busy_retry_after = min(
                max(1, math.ceil(until_free)), settings.login_attempt_lifetime_seconds
            )
        else:
            await db.execute(
                insert(_ATTEMPTS).values(
                    state_hash=sha256_hex(secrets_.state),
                    browser_binding_hash=sha256_hex(secrets_.binding_secret),
                    nonce=secrets_.nonce,
                    pkce_verifier=secrets_.code_verifier,
                    expires_at=func.now()
                    + timedelta(seconds=settings.login_attempt_lifetime_seconds),
                )
            )
    if busy_retry_after is not None:
        log.warning("login attempt cap reached (%s pending)", settings.max_pending_login_attempts)
        raise LoginBusy(busy_retry_after)


async def consume_attempt(
    sessionmaker: async_sessionmaker[AsyncSession], state: str, binding_secret: str
) -> ConsumedAttempt | None:
    """Atomically validate and consume an attempt. Returns None if it is not usable.

    One UPDATE ... RETURNING checks state, browser binding, expiry and single use together, so two
    concurrent callbacks with the same state cannot both succeed (the second sees consumed_at set
    by the first, once the first commits). There is no select-then-update window.

    A binding mismatch does NOT consume the attempt: a third party who learned the state cannot
    burn the real user's login attempt by replaying it from the wrong browser.
    """
    statement = (
        update(_ATTEMPTS)
        .where(
            _ATTEMPTS.c.state_hash == sha256_hex(state),
            _ATTEMPTS.c.browser_binding_hash == sha256_hex(binding_secret),
            _ATTEMPTS.c.consumed_at.is_(None),
            _ATTEMPTS.c.expires_at > func.now(),
        )
        .values(consumed_at=func.now())
        .returning(_ATTEMPTS.c.nonce, _ATTEMPTS.c.pkce_verifier)
    )
    async with sessionmaker() as db, db.begin():
        row = (await db.execute(statement)).one_or_none()
    return ConsumedAttempt(nonce=row.nonce, code_verifier=row.pkce_verifier) if row else None
