"""Collaboration persistence: the ONLY module that writes document_yjs_state, document_snapshots,
collab_lease and documents.collab_state.

The Node collaboration service (collab/) calls these through /internal/collab/*. Rules:

* One transaction per call, opened here from the sessionmaker (as in services/documents.py).
* DOCUMENT ROW LOCKS ONLY. A Yjs store or a snapshot never takes the room lock and never records a
  room event: a store happens every few seconds per active document and must not serialize the room
  or flood the event stream (the room events this feature still emits are document_created,
  document_renamed and active_document_changed, from services/documents.py).
* Every write is FENCED: the caller's lease token is checked with SELECT ... FOR SHARE on the single
  collab_lease row inside the same transaction. FOR SHARE conflicts with the UPDATE that acquiring a
  new lease performs, so a new instance cannot take over between the check and the commit; and
  after it has taken over (token + 1) every write by the old instance fails with stale_lease.
* After a document is 'ready', document_yjs_state is the only writable content. documents.content is
  read-only seed material: nothing here ever updates it (no dual write).
* Nothing here logs content, titles, cookies or the shared secret.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import cookie_parser

from app.config import Settings
from app.errors import AppError, asset_not_found, document_not_found
from app.logging_config import APP_LOGGER
from app.models import (
    CollabLease,
    Document,
    DocumentSnapshot,
    DocumentYjsState,
    Membership,
    RoomAsset,
)
from app.services.documents import sync_asset_refs
from app.services.sessions import load_session

log = logging.getLogger(f"{APP_LOGGER}.collab")

_DOCS = Document.__table__
_STATE = DocumentYjsState.__table__
_SNAPS = DocumentSnapshot.__table__
_LEASE = CollabLease.__table__
_ASSETS = RoomAsset.__table__
_MEMBERS = Membership.__table__

EDIT_ROLES = ("owner", "editor")
WRITE_SNAPSHOT_KINDS = ("seed", "periodic", "final")


# ---- errors (internal API only; the Node service reads `code`) --------------------------------


def stale_lease() -> AppError:
    return AppError(409, "stale_lease", "The collaboration lease is held by another instance.")


def lease_held() -> AppError:
    return AppError(409, "lease_held", "Another collaboration instance holds the lease.")


def stale_counter(current: int) -> AppError:
    return AppError(
        409, "stale_counter", "A newer state is already stored.", extra={"current_counter": current}
    )


def state_too_large() -> AppError:
    return AppError(413, "state_too_large", "The Yjs state is larger than the configured maximum.")


def already_seeded() -> AppError:
    return AppError(409, "already_seeded", "The document already has Yjs state.")


def not_seeded() -> AppError:
    return AppError(409, "not_seeded", "The document has not been seeded into Yjs yet.")


def needs_repair_error() -> AppError:
    return AppError(409, "needs_repair", "The document needs repair.")


# ---- lease ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Lease:
    token: int
    expires_at: datetime


async def acquire_lease(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, holder_id: str
) -> Lease:
    """Take the lease if nobody holds it or it expired. The token increases on EVERY acquisition,
    so a paused old holder is fenced out the moment a new one acquires."""
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(
                text(
                    "INSERT INTO collab_lease (id, fencing_token, holder_id, expires_at)"
                    " VALUES (1, 1, :h, now() + make_interval(secs => :ttl))"
                    " ON CONFLICT (id) DO UPDATE SET"
                    "   fencing_token = collab_lease.fencing_token + 1,"
                    "   holder_id = :h,"
                    "   expires_at = now() + make_interval(secs => :ttl)"
                    " WHERE collab_lease.holder_id IS NULL OR collab_lease.expires_at <= now()"
                    " RETURNING fencing_token, expires_at"
                ),
                {"h": holder_id, "ttl": settings.collab_lease_ttl_seconds},
            )
        ).one_or_none()
    if row is None:
        raise lease_held()
    log.info("collab lease acquired token=%s", row.fencing_token)
    return Lease(row.fencing_token, row.expires_at)


async def renew_lease(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, holder_id: str, token: int
) -> Lease:
    """Extend the lease. Succeeds only for the current token and holder (even if it already expired,
    as long as nobody else acquired: the token has not changed, so nobody else could write)."""
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(
                text(
                    "UPDATE collab_lease SET expires_at = now() + make_interval(secs => :ttl)"
                    " WHERE id = 1 AND fencing_token = :t AND holder_id = :h"
                    " RETURNING fencing_token, expires_at"
                ),
                {"h": holder_id, "t": token, "ttl": settings.collab_lease_ttl_seconds},
            )
        ).one_or_none()
    if row is None:
        raise stale_lease()
    return Lease(row.fencing_token, row.expires_at)


async def release_lease(
    sessionmaker: async_sessionmaker[AsyncSession], holder_id: str, token: int
) -> bool:
    async with sessionmaker() as db, db.begin():
        result = await db.execute(
            text(
                "UPDATE collab_lease SET holder_id = NULL, expires_at = now()"
                " WHERE id = 1 AND fencing_token = :t AND holder_id = :h"
            ),
            {"h": holder_id, "t": token},
        )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def lease_is_free(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """True when no instance holds a live lease (operator commands that need a stopped service)."""
    async with sessionmaker() as db:
        row = (
            await db.execute(
                text("SELECT holder_id IS NULL OR expires_at <= now() AS free FROM collab_lease")
            )
        ).one_or_none()
    return row is None or bool(row.free)


async def _fence(db: AsyncSession, token: int) -> None:
    held = (
        await db.execute(
            text("SELECT 1 FROM collab_lease WHERE id = 1 AND fencing_token = :t FOR SHARE"),
            {"t": token},
        )
    ).scalar_one_or_none()
    if held is None:
        raise stale_lease()


# ---- authorization ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentAccess:
    user_id: uuid.UUID
    display_name: str
    session_id: uuid.UUID
    role: str
    can_edit: bool
    room_id: uuid.UUID
    document_id: uuid.UUID
    collab_state: str


def session_secret_from_cookie_header(settings: Settings, header: str | None) -> str | None:
    """The session cookie's value, parsed from a raw Cookie header. The value is used for one
    lookup and then dropped; it is never returned or logged."""
    if not header or len(header) > 8192:
        return None
    return cookie_parser(header).get(settings.session_cookie_name)


async def authorize_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    cookie_header: str | None,
    document_id: uuid.UUID,
) -> DocumentAccess:
    """Session valid + user active + ACTIVE member of the document's room. 401 session_invalid or
    403 forbidden (also for a document that does not exist: no existence oracle)."""
    secret = session_secret_from_cookie_header(settings, cookie_header)
    async with sessionmaker() as db:
        context = await load_session(db, secret)
        if context is None or context.user.state != "active":
            raise AppError(401, "session_invalid", "Not signed in.")
        row = (
            await db.execute(
                select(_DOCS.c.room_id, _DOCS.c.collab_state, _MEMBERS.c.role)
                .select_from(
                    _DOCS.join(
                        _MEMBERS,
                        (_MEMBERS.c.room_id == _DOCS.c.room_id)
                        & (_MEMBERS.c.user_id == context.user.id)
                        & (_MEMBERS.c.state == "active"),
                    )
                )
                .where(_DOCS.c.id == document_id)
            )
        ).one_or_none()
    if row is None:
        raise AppError(403, "forbidden", "No access to this document.")
    return DocumentAccess(
        user_id=context.user.id,
        display_name=context.user.display_name,
        session_id=context.session_id,
        role=row.role,
        can_edit=row.role in EDIT_ROLES,
        room_id=row.room_id,
        document_id=document_id,
        collab_state=row.collab_state,
    )


# ---- Yjs state --------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredState:
    state: bytes
    update_counter: int
    room_id: uuid.UUID
    collab_state: str


async def load_state(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: uuid.UUID
) -> StoredState | None:
    """The document's collab_state and, if seeded, its Yjs state. None = no such document."""
    async with sessionmaker() as db:
        doc = (
            await db.execute(
                select(_DOCS.c.room_id, _DOCS.c.collab_state).where(_DOCS.c.id == document_id)
            )
        ).one_or_none()
        if doc is None:
            return None
        row = (
            await db.execute(
                select(_STATE.c.state, _STATE.c.update_counter).where(
                    _STATE.c.document_id == document_id
                )
            )
        ).one_or_none()
    if row is None:
        return StoredState(b"", -1, doc.room_id, doc.collab_state)
    return StoredState(bytes(row.state), row.update_counter, doc.room_id, doc.collab_state)


async def store_state(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    token: int,
    document_id: uuid.UUID,
    counter: int,
    state: bytes,
    insert_if_absent: bool,
) -> None:
    """Persist the full Yjs state.

    insert_if_absent=True is SEEDING: INSERT ... ON CONFLICT DO NOTHING, and collab_state becomes
    'ready' in the same transaction. If a row already exists the caller lost the race (or restarted
    mid-seed) and gets 409 already_seeded, then loads what is there. This is what makes "seeded
    exactly once" a property of the database and not of timing.

    Otherwise the row must exist and `counter` must be greater than the stored one (a retry of the
    same attempt, or an older attempt arriving late, is refused with stale_counter).
    """
    if len(state) > settings.collab_max_state_bytes:
        raise state_too_large()
    async with sessionmaker() as db, db.begin():
        await _fence(db, token)
        doc = (
            await db.execute(
                select(_DOCS.c.room_id, _DOCS.c.collab_state)
                .where(_DOCS.c.id == document_id)
                .with_for_update()
            )
        ).one_or_none()
        if doc is None:
            raise document_not_found()
        if insert_if_absent:
            if doc.collab_state == "needs_repair":
                raise needs_repair_error()
            inserted = (
                await db.execute(
                    pg_insert(_STATE)
                    .values(
                        document_id=document_id,
                        room_id=doc.room_id,
                        state=state,
                        update_counter=counter,
                        byte_size=len(state),
                        updated_at=func.clock_timestamp(),
                    )
                    .on_conflict_do_nothing(index_elements=["document_id"])
                    .returning(_STATE.c.document_id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                raise already_seeded()
            await db.execute(
                update(_DOCS).where(_DOCS.c.id == document_id).values(collab_state="ready")
            )
            log.info("collab seeded document=%s bytes=%d", document_id, len(state))
            return
        current = (
            await db.execute(
                select(_STATE.c.update_counter)
                .where(_STATE.c.document_id == document_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            raise not_seeded()
        if counter <= current:
            raise stale_counter(current)
        await db.execute(
            update(_STATE)
            .where(_STATE.c.document_id == document_id)
            .values(
                state=state,
                update_counter=counter,
                byte_size=len(state),
                updated_at=func.clock_timestamp(),
            )
        )
        # "Last edited" for the document list. This is NOT content: documents.content is untouched.
        await db.execute(
            update(_DOCS)
            .where(_DOCS.c.id == document_id)
            .values(content_updated_at=func.clock_timestamp(), updated_at=func.clock_timestamp())
        )


# ---- seed material ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedMaterial:
    format: str  # "interchange" (documents.content) or "tiptap" (a restore snapshot)
    content: dict[str, Any]
    room_id: uuid.UUID


async def get_seed(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: uuid.UUID
) -> SeedMaterial:
    """What a LEGACY document is seeded from: its interchange JSON, unless a restore left a
    'restore' snapshot (plain Tiptap JSON) newer than the content. Anything else is a conflict."""
    async with sessionmaker() as db:
        doc = (
            await db.execute(
                select(_DOCS.c.room_id, _DOCS.c.collab_state, _DOCS.c.content).where(
                    _DOCS.c.id == document_id
                )
            )
        ).one_or_none()
        if doc is None:
            raise document_not_found()
        if doc.collab_state == "ready":
            raise already_seeded()
        if doc.collab_state == "needs_repair":
            raise needs_repair_error()
        restore = (
            await db.execute(
                select(_SNAPS.c.content)
                .where(_SNAPS.c.document_id == document_id, _SNAPS.c.kind == "restore")
                .order_by(_SNAPS.c.created_at.desc(), _SNAPS.c.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if restore is not None:
        return SeedMaterial("tiptap", restore, doc.room_id)
    return SeedMaterial("interchange", doc.content, doc.room_id)


# ---- state marks ------------------------------------------------------------------------------


async def mark_state(
    sessionmaker: async_sessionmaker[AsyncSession],
    token: int,
    document_id: uuid.UUID,
    new_state: str,
    reason: str,
) -> None:
    """legacy/ready -> needs_repair, or needs_repair -> ready (a live document recovered). 'legacy'
    can never be set through the API. The reason is a short code, logged without content."""
    if new_state not in ("ready", "needs_repair"):
        raise AppError(422, "validation_error", "state must be ready or needs_repair.")
    async with sessionmaker() as db, db.begin():
        await _fence(db, token)
        doc = (
            await db.execute(
                select(_DOCS.c.collab_state).where(_DOCS.c.id == document_id).with_for_update()
            )
        ).one_or_none()
        if doc is None:
            raise document_not_found()
        if new_state == "ready":
            has_state = (
                await db.execute(
                    select(_STATE.c.document_id).where(_STATE.c.document_id == document_id)
                )
            ).scalar_one_or_none()
            if has_state is None:  # 'ready' means "Yjs state exists"; never without it
                raise not_seeded()
        await db.execute(
            update(_DOCS).where(_DOCS.c.id == document_id).values(collab_state=new_state)
        )
    log.warning("collab mark document=%s state=%s reason=%s", document_id, new_state, reason[:40])


# ---- snapshots ---------------------------------------------------------------------------------


def asset_ids_in(content: dict[str, Any]) -> set[uuid.UUID]:
    """Every image node's assetId. A malformed id is an error, never silently ignored."""
    found: set[uuid.UUID] = set()
    stack: list[Any] = [content]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("type") == "image":
            raw = (node.get("attrs") or {}).get("assetId")
            try:
                found.add(uuid.UUID(str(raw)))
            except ValueError:
                raise AppError(
                    422, "validation_error", "An image has an invalid assetId."
                ) from None
        stack.extend(node.get("content") or [])
    return found


@dataclass(frozen=True)
class SnapshotResult:
    stored: bool
    snapshot_id: uuid.UUID | None


async def write_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    token: int,
    document_id: uuid.UUID,
    kind: str,
    yjs_update_counter: int,
    content: dict[str, Any],
    content_hash: bytes,
    text_length: int,
) -> SnapshotResult:
    """Store a snapshot the collaboration service has ALREADY validated against the editor schema,
    and make document_asset_refs match it, in one transaction.

    Order after the fence: lock the document row (collab_state must be ready or needs_repair), lock
    the referenced asset rows, refuse (422 asset_not_found) if any is missing from THIS room or
    expired, dedupe against the newest snapshot, insert, prune, sync references. A refused snapshot
    changes nothing. No room lock, no room event.
    """
    if kind not in WRITE_SNAPSHOT_KINDS:
        raise AppError(422, "validation_error", "Unknown snapshot kind.")
    if content.get("type") != "doc":
        raise AppError(422, "validation_error", "A snapshot must be a doc.")
    wanted = asset_ids_in(content)
    async with sessionmaker() as db, db.begin():
        await _fence(db, token)
        doc = (
            await db.execute(
                select(_DOCS.c.room_id, _DOCS.c.collab_state)
                .where(_DOCS.c.id == document_id)
                .with_for_update()
            )
        ).one_or_none()
        if doc is None:
            raise document_not_found()
        if doc.collab_state == "legacy":
            raise not_seeded()
        room_id = doc.room_id
        if wanted:
            found = {
                row.id: row.state
                for row in (
                    await db.execute(
                        select(_ASSETS.c.id, _ASSETS.c.state)
                        .where(_ASSETS.c.room_id == room_id, _ASSETS.c.id.in_(wanted))
                        .order_by(_ASSETS.c.id)
                        .with_for_update()
                    )
                ).all()
            }
            missing = sorted(str(a) for a in wanted if found.get(a) in (None, "expired"))
            if missing:
                raise asset_not_found(missing)
        latest = (
            await db.execute(
                select(_SNAPS.c.content_hash)
                .where(_SNAPS.c.document_id == document_id)
                .order_by(_SNAPS.c.created_at.desc(), _SNAPS.c.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if kind != "seed" and latest is not None and bytes(latest) == content_hash:
            await sync_asset_refs(db, room_id, document_id, wanted)
            return SnapshotResult(False, None)
        snapshot_id = (
            await db.execute(
                insert(_SNAPS)
                .values(
                    document_id=document_id,
                    room_id=room_id,
                    kind=kind,
                    yjs_update_counter=yjs_update_counter,
                    content=content,
                    content_hash=content_hash,
                    text_length=text_length,
                    created_at=func.clock_timestamp(),
                )
                .returning(_SNAPS.c.id)
            )
        ).scalar_one()
        await _prune_snapshots(db, settings, document_id)
        await sync_asset_refs(db, room_id, document_id, wanted)
    log.info(
        "collab snapshot document=%s kind=%s counter=%d images=%d",
        document_id,
        kind,
        yjs_update_counter,
        len(wanted),
    )
    return SnapshotResult(True, snapshot_id)


async def _prune_snapshots(db: AsyncSession, settings: Settings, document_id: uuid.UUID) -> None:
    """Keep the newest N snapshots plus the newest seed (or restore) one, delete the rest."""
    await db.execute(
        text(
            "DELETE FROM document_snapshots WHERE document_id = :d AND id NOT IN ("
            "  (SELECT id FROM document_snapshots WHERE document_id = :d"
            "     ORDER BY created_at DESC, id DESC LIMIT :n)"
            "  UNION"
            "  (SELECT id FROM document_snapshots WHERE document_id = :d"
            "     AND kind IN ('seed','restore')"
            "     ORDER BY created_at DESC, id DESC LIMIT 1))"
        ),
        {"d": document_id, "n": settings.collab_snapshots_kept},
    )


async def check_assets(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, asset_ids: list[uuid.UUID]
) -> list[str]:
    """Ids that do NOT exist (or are expired) in this room (another room's asset is missing)."""
    if not asset_ids:
        return []
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(_ASSETS.c.id).where(
                    _ASSETS.c.room_id == room_id,
                    _ASSETS.c.id.in_(asset_ids),
                    _ASSETS.c.state != "expired",
                )
            )
        ).scalars()
        present = set(rows)
    return sorted(str(a) for a in asset_ids if a not in present)


# ---- operator restore --------------------------------------------------------------------------


async def restore_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    document_id: uuid.UUID,
    snapshot_id: uuid.UUID | None,
) -> tuple[bytes | None, int]:
    """Operator repair. Requires NO live collaboration instance (the lease is free), because this
    deletes the document's Yjs state: it returns the raw state (for a backup file) and the counter.

    The chosen snapshot (default: the newest) is copied into a new 'restore' snapshot and the
    document goes back to 'legacy'. The next time anyone opens it, the collaboration service seeds a
    fresh Yjs document from that snapshot. Nothing is deleted except the old Yjs row.
    """
    async with sessionmaker() as db, db.begin():
        free = (
            await db.execute(
                text("SELECT holder_id IS NULL OR expires_at <= now() FROM collab_lease FOR UPDATE")
            )
        ).scalar_one_or_none()
        if free is False:
            raise AppError(
                409,
                "service_running",
                "Stop the collaboration service before restoring a document.",
            )
        doc = (
            await db.execute(
                select(_DOCS.c.room_id).where(_DOCS.c.id == document_id).with_for_update()
            )
        ).one_or_none()
        if doc is None:
            raise document_not_found()
        query = select(_SNAPS).where(_SNAPS.c.document_id == document_id)
        query = (
            query.where(_SNAPS.c.id == snapshot_id)
            if snapshot_id
            else query.order_by(_SNAPS.c.created_at.desc(), _SNAPS.c.id.desc()).limit(1)
        )
        snap = (await db.execute(query)).one_or_none()
        if snap is None:
            raise AppError(404, "snapshot_not_found", "No snapshot to restore from.")
        old = (
            await db.execute(
                select(_STATE.c.state, _STATE.c.update_counter).where(
                    _STATE.c.document_id == document_id
                )
            )
        ).one_or_none()
        await db.execute(
            insert(_SNAPS).values(
                document_id=document_id,
                room_id=doc.room_id,
                kind="restore",
                yjs_update_counter=0,
                content=snap.content,
                content_hash=snap.content_hash,
                text_length=snap.text_length,
                created_at=func.clock_timestamp(),
            )
        )
        await db.execute(delete(_STATE).where(_STATE.c.document_id == document_id))
        await db.execute(
            update(_DOCS).where(_DOCS.c.id == document_id).values(collab_state="legacy")
        )
    return (bytes(old.state), old.update_counter) if old else (None, -1)
