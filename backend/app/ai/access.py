"""The AI admission gate: every question "may this user use AI here, now?" is answered here.

Order matters, and each failure has its own stable code, so a client can tell the user the real
reason. The membership check (404 room_not_found for outsiders) happens before this, in the route
dependency; everything below runs for an active member.

  1. role           viewer -> 403 insufficient_role
  2. configuration  AI_ENABLED false / settings incomplete   -> 503 ai_not_configured
  3. global switch  operator kill switch                     -> 503 ai_disabled
  4. capability     generation/embedding self-check failing  -> 503 ai_unavailable
  5. room switch    owner turned AI off in this room         -> 409 ai_disabled_for_room
  6. anchor         no document given                        -> 409 ai_anchor_required
  7. can_write      shared decide_write()                    -> 403 ai_write_access_required
  8. consent        current notice version accepted          -> 403 consent_required

`gate()` is called when a request is accepted, again by the worker when it claims a job, before it
publishes, and on retry (later checkpoints): roles, switches and consent can all change in between.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import keys
from app.ai.notice import NOTICE_VERSION
from app.ai.status import load_status
from app.config import Settings
from app.errors import (
    ai_anchor_required,
    ai_disabled,
    ai_disabled_for_room,
    ai_not_configured,
    ai_unavailable,
    ai_write_access_required,
    consent_required,
    document_not_found,
    insufficient_role,
)
from app.models import AiConsent, Room
from app.services.permissions import EDIT_ROLES, WriteDecision, decide_write

_CONSENTS = AiConsent.__table__
_ROOMS = Room.__table__


@dataclass(frozen=True)
class Admission:
    decision: WriteDecision | None
    consent_given: bool
    key_sources: dict[str, keys.KeyChoice]  # keyed by "generation" / "embedding"


async def has_consent(db: AsyncSession, user_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            select(_CONSENTS.c.user_id).where(
                _CONSENTS.c.user_id == user_id, _CONSENTS.c.notice_version == NOTICE_VERSION
            )
        )
    ).first()
    return row is not None


async def gate(
    db: AsyncSession,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    role: str,
    room_id: uuid.UUID,
    document_id: uuid.UUID | None,
    require_consent: bool = True,
) -> Admission:
    if role not in EDIT_ROLES:
        raise insufficient_role()
    status = await load_status(db, settings)
    if status.code == "ai_not_configured":
        raise ai_not_configured()
    if status.code == "ai_disabled":
        raise ai_disabled()
    if status.code == "ai_unavailable":
        failed = next((c.code for c in status.capabilities.values() if c.state == "failed"), "")
        raise ai_unavailable(failed)
    room_on = (
        await db.execute(select(_ROOMS.c.ai_enabled).where(_ROOMS.c.id == room_id))
    ).scalar_one()
    if not room_on:
        raise ai_disabled_for_room()
    if document_id is None:
        raise ai_anchor_required()
    decision = await decide_write(db, settings, user_id, document_id)
    if decision.reason == "document_not_found" or (
        decision.room_id is not None and decision.room_id != room_id
    ):
        # a document of another room is reported like a missing one: no cross-room oracle
        raise document_not_found()
    if not decision.allowed:
        raise ai_write_access_required()
    consent = await has_consent(db, user_id)
    if require_consent and not consent:
        raise consent_required()
    choices = {
        "generation": await keys.choose_key_source(db, settings, user_id, "openai"),
        "embedding": await keys.choose_key_source(db, settings, user_id, "google"),
    }
    return Admission(decision, consent, choices)
