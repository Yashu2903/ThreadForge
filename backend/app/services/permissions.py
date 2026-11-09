"""The ONE write-permission function: `decide_write`.

    can_write(user, document) is true when ALL hold:
      * the user exists and is active
      * the document exists and belongs to a room where the user is an ACTIVE member
      * the member's role is owner or editor
      * the document's collab_state is not 'needs_repair'   (its Yjs state failed validation)
      * the document is not size-locked (its stored Yjs state reached COLLAB_MAX_STATE_BYTES,
        where the collaboration service already forces everybody read-only)
      * the room is not archived  -- ThreadForge has no archived rooms, so this clause has
        nothing to check yet (documented in docs/ai.md); when archiving is added it belongs HERE.

Two callers, one function:
  * the collaboration service's authorize endpoint (services/collab.authorize_document) takes
    `role_ok` (membership and role) for editing. It deliberately does NOT take the needs_repair and
    size clauses: people must still be able to edit a needs_repair document, because that is how it
    heals, and a size-locked document is already read-only inside the service.
  * every AI path takes `allowed` (all clauses), because an AI must never act on, or on behalf of
    someone for, a document that is in a state where humans cannot write.

`decide_write` takes the caller's session and runs ONE read; callers re-run it at each point
the spec names (admission, worker claim, before publish, on retry), because roles change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Document, DocumentYjsState, Membership, User

EDIT_ROLES = ("owner", "editor")
_DOCS = Document.__table__
_MEMBERS = Membership.__table__
_STATE = DocumentYjsState.__table__
_USERS = User.__table__


@dataclass(frozen=True)
class WriteDecision:
    allowed: bool
    reason: str  # "ok" or the FIRST failing clause
    role_ok: bool  # user active + active member + owner/editor + document in that room
    room_id: uuid.UUID | None = None
    role: str | None = None
    collab_state: str | None = None


async def decide_write(
    db: AsyncSession, settings: Settings, user_id: uuid.UUID, document_id: uuid.UUID
) -> WriteDecision:
    row = (
        await db.execute(
            select(
                _USERS.c.state.label("user_state"),
                _DOCS.c.room_id,
                _DOCS.c.collab_state,
                _MEMBERS.c.role,
                _STATE.c.byte_size,
            )
            .select_from(_USERS)
            .outerjoin(_DOCS, _DOCS.c.id == document_id)
            .outerjoin(
                _MEMBERS,
                (_MEMBERS.c.room_id == _DOCS.c.room_id)
                & (_MEMBERS.c.user_id == _USERS.c.id)
                & (_MEMBERS.c.state == "active"),
            )
            .outerjoin(_STATE, _STATE.c.document_id == _DOCS.c.id)
            .where(_USERS.c.id == user_id)
        )
    ).one_or_none()
    if row is None or row.user_state != "active":
        return WriteDecision(False, "user_inactive", False)
    if row.room_id is None:
        return WriteDecision(False, "document_not_found", False)
    if row.role is None:
        return WriteDecision(False, "not_member", False, row.room_id)
    if row.role not in EDIT_ROLES:
        return WriteDecision(
            False, "insufficient_role", False, row.room_id, row.role, row.collab_state
        )
    if row.collab_state == "needs_repair":
        return WriteDecision(False, "needs_repair", True, row.room_id, row.role, row.collab_state)
    if row.byte_size is not None and row.byte_size >= settings.collab_max_state_bytes:
        return WriteDecision(False, "size_locked", True, row.room_id, row.role, row.collab_state)
    return WriteDecision(True, "ok", True, row.room_id, row.role, row.collab_state)
