"""Bring-your-own-key: store, list, delete, resolve, decrypt.

WHO TOUCHES A PLAINTEXT KEY
  * the API process, for the length of ONE validation call in `save_key` (it encrypts and forgets);
  * worker processes, via `load_api_key`, for the length of one provider call.
Nothing else. Plaintext keys are never logged, never returned, never stored; the API returns only
provider, last4 and dates. The ciphertext is bound (AES-GCM AAD) to user_id + provider.

KEY POLICY (AI_KEY_POLICY) decides, per provider, which key a request uses; the choice is made at
admission and recorded with the request. If that exact key is missing or rejected when the request
runs, the request fails (key_unavailable / key_rejected): it NEVER silently switches to the other key.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import errors as perr
from app.ai import ratelimit
from app.ai.crypto import DecryptionError, KeySet, key_aad
from app.ai.factory import Transports, build_embedder, build_llm
from app.ai.spend import CallContext, Spend
from app.ai.types import KeySource, Usage
from app.config import Settings
from app.errors import AppError, key_rejected, key_unavailable, validation_failed
from app.logging_config import APP_LOGGER
from app.models import UserProviderKey

log = logging.getLogger(f"{APP_LOGGER}.ai.keys")
_KEYS = UserProviderKey.__table__
PROVIDERS = ("openai", "google")
_KEY_SHAPE = re.compile(r"[\x21-\x7e]{16,300}")  # printable ASCII, no spaces, sane length


@dataclass(frozen=True)
class KeyMeta:
    provider: str
    last4: str
    state: str
    created_at: datetime
    last_validated_at: datetime | None


@dataclass(frozen=True)
class KeyChoice:
    provider: str
    source: KeySource | None  # None = nothing usable
    problem: str | None = None  # key_unavailable | key_rejected when source is None


def _keyset(settings: Settings) -> KeySet:
    raw = settings.ai_key_encryption_keys.get_secret_value()
    if not raw:
        raise AppError(
            503, "ai_not_configured", "Storing API keys is not configured on this server."
        )
    return KeySet.parse(raw)


def server_key(settings: Settings, provider: str) -> str:
    value = settings.ai_openai_api_key if provider == "openai" else settings.ai_google_api_key
    return value.get_secret_value()


async def list_keys(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> list[KeyMeta]:
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(_KEYS).where(_KEYS.c.user_id == user_id).order_by(_KEYS.c.provider)
            )
        ).all()
    return [KeyMeta(r.provider, r.last4, r.state, r.created_at, r.last_validated_at) for r in rows]


async def delete_key(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: uuid.UUID, provider: str
) -> bool:
    async with sessionmaker() as db, db.begin():
        result = await db.execute(
            delete(_KEYS).where(_KEYS.c.user_id == user_id, _KEYS.c.provider == provider)
        )
    return result.rowcount > 0


async def save_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    provider: str,
    plaintext: str,
    client_ip: str,
    *,
    transports: Transports | None = None,
) -> KeyMeta:
    """Rate-limit, validate with ONE cheap provider call, then store encrypted. The plaintext exists
    only in this function's locals."""
    if not settings.ai_enabled:
        raise AppError(503, "ai_not_configured", "AI is not configured on this server.")
    if settings.ai_key_policy == "server_only":
        raise AppError(
            409, "user_keys_not_allowed", "This server does not accept personal API keys."
        )
    if provider not in PROVIDERS:
        raise validation_failed("Unknown provider.")
    key = plaintext.strip()
    if not _KEY_SHAPE.fullmatch(key):
        raise validation_failed("That does not look like an API key.")
    keyset = _keyset(settings)

    # Count the attempt FIRST (committed), so key-guessing is throttled whatever the outcome.
    await ratelimit.consume(
        sessionmaker,
        f"keyval:user:{user_id}",
        settings.ai_key_validations_per_user_per_hour,
        message="Too many key checks. Try again later.",
    )
    await ratelimit.consume(
        sessionmaker,
        f"keyval:ip:{client_ip}",
        settings.ai_key_validations_per_ip_per_hour,
        message="Too many key checks from this network. Try again later.",
    )

    ctx = CallContext(
        user_id=user_id,
        charged_user_id=user_id,
        room_id=None,
        provider=provider,
        model=settings.ai_llm_model if provider == "openai" else settings.ai_embed_model,
        key_source="user",
    )
    spend = Spend(sessionmaker, settings)
    adapter = (
        build_llm(settings, key, transports)
        if provider == "openai"
        else build_embedder(settings, key, transports)
    )
    try:
        try:
            await adapter.validate_key()
        except perr.ProviderError as exc:
            await spend.record(ctx, "key_validate", Usage(), 0, "failed", error_code=exc.code)
            if exc.code == perr.KEY_REJECTED:
                raise key_rejected(provider) from None
            if exc.code == "model_unavailable":
                raise AppError(
                    422,
                    "model_unavailable",
                    "This key cannot use the configured model.",
                    extra={"provider": provider},
                ) from None
            raise AppError(
                502, "provider_unavailable", "The provider could not be reached to check the key."
            ) from None
        await spend.record(ctx, "key_validate", Usage(), 0, "ok")
    finally:
        await adapter.aclose()

    key_id, blob = keyset.encrypt(key.encode(), key_aad(user_id, provider))
    values = {
        "user_id": user_id,
        "provider": provider,
        "encrypted_key": blob,
        "key_id": key_id,
        "last4": key[-4:],
        "state": "active",
        "last_validated_at": func.now(),
        "created_at": func.now(),
    }
    async with sessionmaker() as db, db.begin():
        stmt = pg_insert(_KEYS).values(**values)
        row = (
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["user_id", "provider"],
                    set_={
                        k: stmt.excluded[k]
                        for k in (
                            "encrypted_key",
                            "key_id",
                            "last4",
                            "state",
                            "last_validated_at",
                            "created_at",
                        )
                    },
                ).returning(_KEYS)
            )
        ).one()
    log.info("ai key saved provider=%s user=%s", provider, user_id)
    return KeyMeta(row.provider, row.last4, row.state, row.created_at, row.last_validated_at)


async def choose_key_source(
    db: AsyncSession, settings: Settings, user_id: uuid.UUID, provider: str
) -> KeyChoice:
    """The admission-time decision for ONE provider (see the module docstring)."""
    policy = settings.ai_key_policy
    has_server = bool(server_key(settings, provider))
    row = None
    if policy != "server_only":
        row = (
            await db.execute(
                select(_KEYS.c.state).where(
                    _KEYS.c.user_id == user_id, _KEYS.c.provider == provider
                )
            )
        ).one_or_none()
    if row is not None:
        # a saved personal key wins under user_if_present, and is the only choice under user_only;
        # if it was rejected the request must say so, not fall back to the server's key
        if row.state == "active":
            return KeyChoice(provider, "user")
        return KeyChoice(provider, None, "key_rejected")
    if policy == "user_only":
        return KeyChoice(provider, None, "key_unavailable")
    if has_server:
        return KeyChoice(provider, "server")
    return KeyChoice(provider, None, "key_unavailable")


async def load_api_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    provider: str,
    source: KeySource,
) -> str:
    """WORKERS ONLY. The plaintext key for the source decided at admission, or key_unavailable /
    key_rejected. Never substitutes the other source."""
    if source == "server":
        value = server_key(settings, provider)
        if not value:
            raise key_unavailable(provider)
        return value
    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(_KEYS).where(_KEYS.c.user_id == user_id, _KEYS.c.provider == provider)
            )
        ).one_or_none()
    if row is None:
        raise key_unavailable(provider)
    if row.state != "active":
        raise key_rejected(provider)
    try:
        return (
            _keyset(settings)
            .decrypt(row.key_id, bytes(row.encrypted_key), key_aad(user_id, provider))
            .decode()
        )
    except (DecryptionError, UnicodeDecodeError):
        log.error("ai key could not be decrypted provider=%s user=%s", provider, user_id)
        raise key_unavailable(provider) from None


async def mark_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: uuid.UUID, provider: str
) -> None:
    """A worker saw the provider reject this key (401/403): remember it, so the next request fails
    fast with key_rejected and the UI can ask for a new key."""
    async with sessionmaker() as db, db.begin():
        await db.execute(
            update(_KEYS)
            .where(_KEYS.c.user_id == user_id, _KEYS.c.provider == provider)
            .values(state="rejected")
        )
