"""AI proposals: reading them (checkpoint 2). Applying, rejecting, regenerating and publishing arrive with
checkpoint 4; the rows themselves are written only by `ai_requests.publish_success`, in the same transaction
as the result and the chat reply.

A proposal is a REQUEST to change something, never a change. Reading one touches nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AiProposal
from app.services.permissions import EDIT_ROLES, decide_write

_PROP = AiProposal.__table__
VISIBLE_ROLES = EDIT_ROLES


@dataclass(frozen=True)
class ProposalRow:
    row: Any

    @property
    def effective_state(self) -> str:
        """`pending` past its expiry is reported as `expired` without waiting for the sweeper."""
        r = self.row
        if r.state == "pending" and r.expires_at <= datetime.now(UTC):
            return "expired"
        return str(r.state)


async def get_proposal(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, proposal_id: uuid.UUID
) -> ProposalRow | None:
    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(_PROP).where(_PROP.c.id == proposal_id, _PROP.c.room_id == room_id)
            )
        ).one_or_none()
    return ProposalRow(row) if row is not None else None


async def may_view(
    db: AsyncSession, settings: Any, *, role: str, user_id: uuid.UUID, row: Any
) -> bool:
    """The requester, and any owner or editor with CURRENT write access to the target document. For a
    draft (no document): the requester and the owner(s). Always an owner or editor of the room: a viewer
    sees only the ordinary chat message."""
    if role not in VISIBLE_ROLES:
        return False
    if row.created_by == user_id:
        return True
    if row.kind == "draft" or row.document_id is None:
        return role == "owner"
    decision = await decide_write(db, settings, user_id, row.document_id)
    return decision.allowed
