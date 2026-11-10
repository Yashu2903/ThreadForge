"""AI proposals: reading them, and every change to their state.

A proposal is a REQUEST to change something, never a change. Its rows are born in
`ai_requests.publish_success` (with the result and the chat reply, one transaction); everything that happens to
one afterwards happens here:

    pending --apply--> applying --> applied            (the collaboration service changed the document)
       |                    |-----> conflicted         (the document no longer matches; nothing changed)
       |                    '-----> pending            (document busy / failed before any change)
       |--reject--> rejected
       |--regenerate (from conflicted) --> superseded  (a new request replaced it)
       |--publish (drafts, owner) --> applied          (a new document was created from it)
       '--time--> expired

Rules that hold for every transition:
  * LOCK ORDER is the room row first, then the proposal row (the same order as requests), and every transition
    records its `ai_proposal_updated` event in the same transaction;
  * `applying` is taken with a compare-and-set, and only `pending` can be rejected, expired or superseded, so an
    apply in flight can never be pulled out from under the collaboration service;
  * the collaboration-service entry points (`begin_apply`, `release_apply`, `mark_conflicted`, `finish_apply`)
    check the FENCING token inside their transaction, so a stale instance is refused.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import (
    ai_proposal_not_found,
    ai_write_access_required,
    document_not_found,
    insufficient_role,
    proposal_not_pending,
)
from app.logging_config import APP_LOGGER
from app.models import AiProposal, AiProposalApplication
from app.services.collab import require_fence
from app.services.permissions import EDIT_ROLES, decide_write
from app.services.room_events import record_room_event
from app.services.room_lock import LockedRoom, lock_room
from app.services.rooms import lock_room_and_actor

log = logging.getLogger(f"{APP_LOGGER}.ai.proposals")
_PROP = AiProposal.__table__
_APP = AiProposalApplication.__table__
VISIBLE_ROLES = EDIT_ROLES
OPEN_STATES = ("pending", "applying")


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


@dataclass(frozen=True)
class Permissions:
    """What THIS person may do with THIS proposal right now. The server decides; the card only asks."""

    can_apply: bool = False
    can_reject: bool = False
    can_regenerate: bool = False
    can_publish: bool = False


async def permissions_for(
    db: AsyncSession, settings: Any, *, role: str, user_id: uuid.UUID, view: ProposalRow
) -> Permissions:
    row, state = view.row, view.effective_state
    if role not in VISIBLE_ROLES:
        return Permissions()
    requester = row.created_by == user_id
    writable = False
    if row.document_id is not None:
        writable = (await decide_write(db, settings, user_id, row.document_id)).allowed
    if row.kind == "draft":
        pending = state == "pending"
        return Permissions(
            can_reject=pending and (requester or role == "owner"),
            can_publish=pending and role == "owner",
        )
    return Permissions(
        can_apply=state == "pending" and writable,
        can_reject=state == "pending" and (requester or writable),
        can_regenerate=state == "conflicted" and writable,
    )


# ---- locking, events -------------------------------------------------------------------------------------------


async def _lock(db: AsyncSession, proposal_id: uuid.UUID) -> tuple[LockedRoom, Any]:
    """The ROOM row, then the proposal row (the documented lock order). 404 for an unknown proposal."""
    room_id = (
        await db.execute(select(_PROP.c.room_id).where(_PROP.c.id == proposal_id))
    ).scalar_one_or_none()
    locked = await lock_room(db, room_id) if room_id is not None else None
    if locked is None:
        raise ai_proposal_not_found()
    row = (
        await db.execute(select(_PROP).where(_PROP.c.id == proposal_id).with_for_update())
    ).one_or_none()
    if row is None:
        raise ai_proposal_not_found()
    return locked, row


async def emit(
    db: AsyncSession, locked: LockedRoom, proposal_id: uuid.UUID, state: str, actor_id: uuid.UUID
) -> None:
    await record_room_event(
        db,
        locked,
        event_type="ai_proposal_updated",
        resource_type="ai_proposal",
        resource_id=proposal_id,
        resource_version=None,
        actor_id=actor_id,
        payload={"proposal_id": str(proposal_id), "state": state},
    )


# ---- applying (called by the collaboration service, under its fenced lease) -----------------------------------------
#
# The collaboration service changes the document; THIS module is the authority on whether it may.


async def begin_apply(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    token: int,
    *,
    proposal_id: uuid.UUID,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """Recheck EVERYTHING now, then move pending -> applying (compare-and-set). The checks, in order: the
    fencing token is current; the proposal exists and targets THIS document (a mismatch is "not found": no
    oracle); the applying user has write access to the document right now (role, membership, document state);
    then by state: applied -> report it (idempotent); applying by someone else -> refuse; applying by the same
    user -> resume; pending and unexpired -> take it; anything else -> refuse with its state."""
    expired = False
    async with sessionmaker() as db, db.begin():
        await require_fence(db, token)  # a stale collaboration instance is refused here
        locked, row = await _lock(db, proposal_id)
        if row.document_id != document_id or row.kind != "edit":
            raise ai_proposal_not_found()
        decision = await decide_write(db, settings, user_id, document_id)
        if decision.reason == "document_not_found" or decision.room_id != row.room_id:
            raise document_not_found()
        if not decision.allowed:
            raise ai_write_access_required()
        if row.state == "applied":
            counter = (
                await db.execute(
                    select(_APP.c.resulting_counter).where(_APP.c.proposal_id == proposal_id)
                )
            ).scalar_one_or_none()
            return _begin_payload(row, already=True, counter=counter)
        if row.state == "applying":
            if row.applying_by != user_id:
                raise proposal_not_pending("applying")
            return _begin_payload(row)
        if row.state != "pending":
            raise proposal_not_pending(row.state)
        past = (await db.execute(text("SELECT now() >= :e"), {"e": row.expires_at})).scalar_one()
        if past:
            await db.execute(
                update(_PROP)
                .where(_PROP.c.id == proposal_id, _PROP.c.state == "pending")
                .values(state="expired", resolved_at=func.now(), resolution_code="expired")
            )
            await emit(db, locked, proposal_id, "expired", user_id)
            expired = True
        else:
            await db.execute(
                update(_PROP)
                .where(_PROP.c.id == proposal_id, _PROP.c.state == "pending")
                .values(state="applying", applying_by=user_id, applying_since=func.now())
            )
            await emit(db, locked, proposal_id, "applying", user_id)
            return _begin_payload(row)
    if expired:
        raise proposal_not_pending("expired")
    raise AssertionError("unreachable")  # pragma: no cover


def _begin_payload(
    row: Any, *, already: bool = False, counter: int | None = None
) -> dict[str, Any]:
    return {
        "already_applied": already,
        "proposal_id": str(row.id),
        "document_id": str(row.document_id),
        "room_id": str(row.room_id),
        "operations": row.operations,
        "base_hashes": row.base_hashes,
        "scope": row.scope,
        "captured_counter": row.captured_counter,
        "resulting_counter": counter,
    }


async def release_apply(
    sessionmaker: async_sessionmaker[AsyncSession], token: int, proposal_id: uuid.UUID, reason: str
) -> bool:
    """applying -> pending, because nothing was changed (the document was busy, or the apply failed
    before the change). Idempotent: any other state is left alone."""
    async with sessionmaker() as db, db.begin():
        await require_fence(db, token)
        locked, row = await _lock(db, proposal_id)
        if row.state != "applying":
            return False
        await db.execute(
            update(_PROP)
            .where(_PROP.c.id == proposal_id, _PROP.c.state == "applying")
            .values(state="pending", applying_by=None, applying_since=None)
        )
        await emit(db, locked, proposal_id, "pending", row.applying_by or row.created_by)
    log.info("action=proposal_released proposal_id=%s reason=%s", proposal_id, reason[:40])
    return True


async def mark_conflicted(
    sessionmaker: async_sessionmaker[AsyncSession],
    token: int,
    proposal_id: uuid.UUID,
    user_id: uuid.UUID,
    code: str,
) -> bool:
    """applying -> conflicted: the live document no longer matches what the proposal was based on. The
    document was not changed. The person can regenerate from the current content."""
    async with sessionmaker() as db, db.begin():
        await require_fence(db, token)
        locked, row = await _lock(db, proposal_id)
        if row.state != "applying":
            return False
        await db.execute(
            update(_PROP)
            .where(_PROP.c.id == proposal_id, _PROP.c.state == "applying")
            .values(
                state="conflicted",
                resolved_by=user_id,
                resolved_at=func.now(),
                resolution_code=code[:48],
                applying_by=None,
                applying_since=None,
            )
        )
        await emit(db, locked, proposal_id, "conflicted", user_id)
    log.info("action=proposal_conflicted proposal_id=%s code=%s", proposal_id, code[:40])
    return True


async def finish_apply(
    sessionmaker: async_sessionmaker[AsyncSession],
    token: int,
    proposal_id: uuid.UUID,
    user_id: uuid.UUID,
    resulting_counter: int,
    receipt_hash: bytes,
) -> str:
    """applying -> applied, AFTER the Yjs state and its receipt committed. The receipt row is normally already
    there (written by the store); if the store had nothing new to write (a repeat), it is recorded here.
    Idempotent: an applied proposal stays applied."""
    async with sessionmaker() as db, db.begin():
        await require_fence(db, token)
        locked, row = await _lock(db, proposal_id)
        if row.state == "applied":
            return "applied"
        if row.state != "applying":
            raise proposal_not_pending(row.state)
        who = row.applying_by or user_id
        await db.execute(
            pg_insert(_APP)
            .values(
                proposal_id=proposal_id,
                applied_by=who,
                resulting_counter=resulting_counter,
                receipt_hash=receipt_hash,
            )
            .on_conflict_do_nothing(index_elements=["proposal_id"])
        )
        await db.execute(
            update(_PROP)
            .where(_PROP.c.id == proposal_id)
            .values(
                state="applied",
                resolved_by=who,
                resolved_at=func.now(),
                resolution_code="applied",
                applying_by=None,
                applying_since=None,
            )
        )
        await emit(db, locked, proposal_id, "applied", who)
    log.info("action=proposal_applied proposal_id=%s counter=%s", proposal_id, resulting_counter)
    return "applied"


async def stuck_proposals(
    sessionmaker: async_sessionmaker[AsyncSession], older_than_seconds: int
) -> list[dict[str, str]]:
    """Proposals that have been `applying` for longer than this: the recovery sweeper's work list."""
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                text(
                    """SELECT id, document_id, applying_by FROM ai_proposals
                        WHERE state = 'applying'
                          AND applying_since < now() - make_interval(secs => :s)
                        ORDER BY applying_since LIMIT 50"""
                ),
                {"s": older_than_seconds},
            )
        ).all()
    return [
        {
            "proposal_id": str(r.id),
            "document_id": str(r.document_id),
            "applying_by": str(r.applying_by),
        }
        for r in rows
    ]


# ---- people's decisions -------------------------------------------------------------------------------------------


async def reject(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    proposal_id: uuid.UUID,
) -> str:
    """Only a PENDING proposal can be rejected (not one being applied). Rejecting changes nothing else.
    The requester, or for an edit anyone with current write access, or for a draft the owner. Rejecting an
    already rejected proposal is a no-op."""
    async with sessionmaker() as db, db.begin():
        locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        row = (
            await db.execute(
                select(_PROP)
                .where(_PROP.c.id == proposal_id, _PROP.c.room_id == room_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ai_proposal_not_found()
        if role not in EDIT_ROLES:
            raise insufficient_role()
        view = ProposalRow(row)
        perms = await permissions_for(db, settings, role=role, user_id=actor_id, view=view)
        if row.state == "rejected":
            return "rejected"
        if view.effective_state != "pending":
            raise proposal_not_pending(view.effective_state)
        if not perms.can_reject:
            raise ai_write_access_required()
        await db.execute(
            update(_PROP)
            .where(_PROP.c.id == proposal_id, _PROP.c.state == "pending")
            .values(
                state="rejected",
                resolved_by=actor_id,
                resolved_at=func.now(),
                resolution_code="rejected",
            )
        )
        await emit(db, locked, proposal_id, "rejected", actor_id)
    log.info("action=proposal_rejected proposal_id=%s actor_id=%s", proposal_id, actor_id)
    return "rejected"


async def supersede(
    sessionmaker: async_sessionmaker[AsyncSession], actor_id: uuid.UUID, proposal_id: uuid.UUID
) -> bool:
    """conflicted -> superseded: a regenerated request replaced it. The old preview stays readable."""
    async with sessionmaker() as db, db.begin():
        locked, row = await _lock(db, proposal_id)
        if row.state != "conflicted":
            return False
        await db.execute(
            update(_PROP)
            .where(_PROP.c.id == proposal_id, _PROP.c.state == "conflicted")
            .values(state="superseded", resolution_code="regenerated")
        )
        await emit(db, locked, proposal_id, "superseded", actor_id)
    return True


async def expire_due(sessionmaker: async_sessionmaker[AsyncSession], batch: int = 50) -> int:
    """pending -> expired once `expires_at` has passed (the API already REPORTS it as expired; this makes it
    true in the database, once, with its event). Returns how many."""
    async with sessionmaker() as db:
        due = (
            (
                await db.execute(
                    text(
                        "SELECT id FROM ai_proposals WHERE state = 'pending' AND expires_at <= now()"
                        " ORDER BY expires_at LIMIT :n"
                    ),
                    {"n": batch},
                )
            )
            .scalars()
            .all()
        )
    done = 0
    for proposal_id in due:
        async with sessionmaker() as db, db.begin():
            locked, row = await _lock(db, proposal_id)
            if row.state != "pending":
                continue
            still = (
                await db.execute(text("SELECT now() >= :e"), {"e": row.expires_at})
            ).scalar_one()
            if not still:
                continue
            await db.execute(
                update(_PROP)
                .where(_PROP.c.id == proposal_id, _PROP.c.state == "pending")
                .values(state="expired", resolved_at=func.now(), resolution_code="expired")
            )
            await emit(db, locked, proposal_id, "expired", row.created_by)
            done += 1
    return done


async def delete_old(
    sessionmaker: async_sessionmaker[AsyncSession], retention_days: int, batch: int = 200
) -> int:
    """Resolved proposals (with their before-copies and receipts) are kept for `retention_days`, then deleted.
    A pending or applying proposal is never deleted."""
    total = 0
    while True:
        async with sessionmaker() as db, db.begin():
            done = (
                await db.execute(
                    text(
                        """DELETE FROM ai_proposals WHERE id IN (
                               SELECT id FROM ai_proposals
                                WHERE state NOT IN ('pending', 'applying')
                                  AND resolved_at < now() - make_interval(days => :d)
                                LIMIT :n)"""
                    ),
                    {"d": retention_days, "n": batch},
                )
            ).rowcount
        total += done
        if done < batch:
            return total


# ---- publishing a draft ---------------------------------------------------------------------------------------------


def draft_check(row: Any, now_expired: bool) -> None:
    if row.kind != "draft":
        raise proposal_not_pending(row.state)  # an edit proposal is applied, not published
    if row.state != "pending" or now_expired:
        raise proposal_not_pending(
            "expired" if now_expired and row.state == "pending" else row.state
        )


PublishHook = Callable[[AsyncSession, LockedRoom, uuid.UUID], Awaitable[None]]


async def publish_draft(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    proposal_id: uuid.UUID,
    client_request_id: uuid.UUID,
) -> tuple[Any, bool]:
    """Owner only. Creates the draft's document through the EXISTING create-document path (so it keeps the
    atomic room events, the outbox and the per-room limits) and, in the SAME transaction, links the proposal
    to it and marks it applied. The new document becomes the room's active document (the card said so).

    A retry with the same `client_request_id` finds the document that request already made and changes nothing
    (the proposal row is checked only when a document is actually created). Two different request ids cannot
    make two documents: the second finds the proposal already published. A draft is never published by
    anything but this call."""
    from app.services import documents  # late: documents does not know about proposals

    view = await get_proposal(sessionmaker, room_id, proposal_id)
    if view is None:
        raise ai_proposal_not_found()
    row = view.row
    if row.kind != "draft":
        raise proposal_not_pending(row.state)
    document = (row.preview or {}).get("document")
    title = (row.operations or {}).get("title") if isinstance(row.operations, dict) else None
    if not isinstance(document, dict) or not isinstance(title, str):
        raise proposal_not_pending(row.state)  # its content was removed (the card was deleted)

    async def before(db: AsyncSession, locked: LockedRoom) -> None:
        _, current = await _lock(db, proposal_id)
        expired = (
            await db.execute(text("SELECT now() >= :e"), {"e": current.expires_at})
        ).scalar_one()
        draft_check(current, bool(expired))

    async def after(db: AsyncSession, locked: LockedRoom, document_id: uuid.UUID) -> None:
        await db.execute(
            update(_PROP)
            .where(_PROP.c.id == proposal_id, _PROP.c.state == "pending")
            .values(
                state="applied",
                resolved_by=actor_id,
                resolved_at=func.now(),
                resolution_code="published",
                published_document_id=document_id,
            )
        )
        await emit(db, locked, proposal_id, "applied", actor_id)

    result = await documents.create_document(
        sessionmaker,
        settings,
        actor_id,
        room_id,
        title,
        "ai_draft",
        client_request_id,
        content=document,
        seed_format="tiptap",
        extra_fingerprint=str(proposal_id),
        before_insert=before,
        after_insert=after,
    )
    return result.document, result.created


async def regenerate(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    proposal_id: uuid.UUID,
    client_request_id: uuid.UUID,
) -> Any:
    """From a CONFLICTED edit proposal: a new request for the same instruction, files, chat context and target
    blocks, read from the document's CURRENT content, linked to the old proposal. The old preview stays
    readable and is marked superseded. All the usual admission checks apply to the new request."""
    from app.services import ai_requests as requests

    async with sessionmaker() as db:
        role = (
            await db.execute(
                text(
                    "SELECT role FROM memberships WHERE room_id = :r AND user_id = :u AND state = 'active'"
                ),
                {"r": room_id, "u": actor_id},
            )
        ).scalar_one_or_none()
        row = (
            await db.execute(
                select(_PROP).where(_PROP.c.id == proposal_id, _PROP.c.room_id == room_id)
            )
        ).one_or_none()
        if row is None or role is None:
            raise ai_proposal_not_found()
        if role not in EDIT_ROLES:
            raise insufficient_role()
        if row.kind != "edit" or row.state != "conflicted":
            raise proposal_not_pending(row.state)
        perms = await permissions_for(
            db, settings, role=role, user_id=actor_id, view=ProposalRow(row)
        )
        if not perms.can_regenerate:
            raise ai_write_access_required()
        req = (
            await db.execute(
                text(
                    """SELECT instruction, document_id, selected_block_ids, selected_ranges, insert_at,
                              chat_message_ids, include_chat_context
                         FROM ai_requests WHERE id = :i"""
                ),
                {"i": row.request_id},
            )
        ).one()
        pins = (
            (
                await db.execute(
                    text(
                        "SELECT source_version_id FROM ai_request_sources WHERE request_id = :i ORDER BY position"
                    ),
                    {"i": row.request_id},
                )
            )
            .scalars()
            .all()
        )
    view, _created = await requests.create_request(
        sessionmaker,
        settings,
        actor_id,
        room_id,
        requests.RequestInput(
            client_request_id=client_request_id,
            instruction=req.instruction,
            task_hint="ask",
            document_id=req.document_id,
            include_document_content=True,
            document_scope="edit",
            selected_block_ids=tuple(uuid.UUID(str(i)) for i in req.selected_block_ids),
            selection_hash=None,  # the content changed: the blocks are re-read, not re-hashed
            source_version_ids=tuple(pins),
            include_chat_context=bool(req.chat_message_ids) and req.include_chat_context,
            chat_message_ids=tuple(uuid.UUID(str(i)) for i in req.chat_message_ids),
            confirm_large=True,
            operation="edit",
            selected_ranges=tuple(req.selected_ranges or ()),
            insert_at=req.insert_at,
            regenerate_of=proposal_id,
        ),
    )
    await supersede(sessionmaker, actor_id, proposal_id)
    return view
