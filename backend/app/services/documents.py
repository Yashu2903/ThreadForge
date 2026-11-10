"""Documents: the ONLY module that mutates `documents` rows, document_asset_refs and the
rooms.active_document_* columns.

A document's CONTENT is not handled here any more. Since the collaboration change the text lives in
the Yjs state owned by the collaboration service (services/collab.py is the only writer), and this
module deals in metadata: creating (which also stores the template as read-only SEED content in
documents.content), listing, renaming and choosing the room's active document.
`documents.content`, `content_version`, `content_hash` and `document_write_buckets` are DEPRECATED
columns/tables: kept, never written after creation, never returned.

TRANSACTION PATTERN (Parts 2 and 3, unchanged): a service takes the sessionmaker, never an
existing session; it opens its own session and ONE transaction (the `async with ... db.begin()`
block), which commits at block exit. READ COMMITTED stays the isolation level: every in-lock lookup
relies on each statement seeing the latest committed data.

LOCK ORDER for every write:
    1. room row           SELECT ... FOR NO KEY UPDATE   (lock_room_and_actor)
    2. document row       SELECT ... FOR UPDATE          (rename)
Because the room lock is held first, the caller's membership and role cannot change underneath the
request, and the room's event sequence can be allocated (record_room_event).

Every change that matters to other people records a room event in the SAME transaction (events carry
ids and versions, never titles or content). Responses are built AFTER the commit. Nothing here talks
to the network; nothing logs titles or content.
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, exists, func, insert, literal, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import db_error_details
from app.documents.canonical import content_hash
from app.documents.schema import Limits
from app.documents.templates import template_document
from app.errors import (
    document_limit_reached,
    document_not_found,
    document_request_id_reused,
    selection_conflict,
)
from app.logging_config import APP_LOGGER, request_id_var
from app.models import Document, DocumentAssetRef, Room, RoomAsset
from app.pagination import decode_cursor, encode_cursor
from app.services.room_events import record_room_event
from app.services.rooms import lock_room_and_actor

log = logging.getLogger(f"{APP_LOGGER}.documents")

_DOCS = Document.__table__
_ROOMS = Room.__table__
_REFS = DocumentAssetRef.__table__
_ASSETS = RoomAsset.__table__

UNIQUE_VIOLATION = "23505"
REQUEST_ID_CONSTRAINT = "uq_documents_room_creator_request"
FINGERPRINT_VERSION = b"doc-create-v1"
EDIT_ROLES = ("owner", "editor")


def limits_from_settings(settings: Settings) -> Limits:
    return Limits(
        max_serialized_bytes=settings.document_max_serialized_bytes,
        max_text_codepoints=settings.document_max_text_codepoints,
        max_total_nodes=settings.document_max_total_nodes,
        max_blocks=settings.document_max_blocks,
        max_inline_nodes=settings.document_max_inline_nodes,
        max_images=settings.document_max_images,
        max_list_depth=settings.document_max_list_depth,
        max_link_length=settings.document_max_link_length,
        allowed_fonts=frozenset(settings.document_font_keys),
    )


@dataclass(frozen=True)
class DocumentSummary:
    """Metadata only. A document's CONTENT is never returned by this service: it lives in the Yjs
    state owned by the collaboration service (docs/collaboration.md)."""

    id: uuid.UUID
    room_id: uuid.UUID
    title: str
    template_key: str
    schema_version: int
    metadata_version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    is_active: bool
    requires_review: bool
    collab_state: str  # legacy | ready | needs_repair


@dataclass(frozen=True)
class CreateResult:
    document: DocumentSummary
    created: bool  # True: a new row (201). False: a matching retry of an earlier create (200).


@dataclass(frozen=True)
class Selection:
    active_document_id: uuid.UUID | None
    active_document_version: int


def log_document_event(
    action: str, room_id: uuid.UUID, document_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """Structured line: ids only, never titles or content."""
    log.info(
        "action=%s room_id=%s document_id=%s actor_id=%s request_id=%s",
        action,
        room_id,
        document_id,
        actor_id,
        request_id_var.get(),
    )


# ---- request fingerprint ------------------------------------------------------------------------


def compute_create_fingerprint(
    settings: Settings, title: str, template_key: str, extra: str = ""
) -> bytes:
    """HMAC-SHA256 over a versioned, deterministic serialization of the creation fields.

    Keyed (with the existing fingerprint secret) so the stored value is not an offline-checkable
    hash of a title. A retry of create with the SAME client_request_id and the same fields answers
    with the existing document; the same id with different fields is document_request_id_reused.
    Compared in constant time.
    """
    import hashlib

    canonical = json.dumps(
        {"title": title, "template_key": template_key, **({"extra": extra} if extra else {})},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    key = settings.message_fingerprint_secret.get_secret_value().encode("utf-8")
    return hmac.new(key, FINGERPRINT_VERSION + b"\n" + canonical, hashlib.sha256).digest()


# ---- shared building blocks ----------------------------------------------------------------------


def _summary_columns():
    return [
        _DOCS.c.id,
        _DOCS.c.room_id,
        _DOCS.c.title,
        _DOCS.c.template_key,
        _DOCS.c.schema_version,
        _DOCS.c.metadata_version,
        _DOCS.c.created_by,
        _DOCS.c.created_at,
        _DOCS.c.updated_at,
        _DOCS.c.collab_state,
        (_ROOMS.c.active_document_id == _DOCS.c.id).label("is_active"),
        # Imports (a later checkpoint) set this from document_imports; no import can exist yet.
        literal(False).label("requires_review"),
    ]


def _joined(*extra):
    return select(*_summary_columns(), *extra).select_from(
        _DOCS.join(_ROOMS, _ROOMS.c.id == _DOCS.c.room_id)
    )


def _summary(row) -> DocumentSummary:
    return DocumentSummary(
        **{k: v for k, v in row._mapping.items() if k in DocumentSummary.__dataclass_fields__}
    )


async def _lock_document(db: AsyncSession, room_id: uuid.UUID, document_id: uuid.UUID):
    """Lock-order step 2: the document row FOR UPDATE, scoped by BOTH room and id (another room's
    document id is simply not found)."""
    return (
        await db.execute(
            select(
                _DOCS.c.id,
                _DOCS.c.title,
                _DOCS.c.content_version,
                _DOCS.c.metadata_version,
                _DOCS.c.content_hash,
            )
            .where(_DOCS.c.id == document_id, _DOCS.c.room_id == room_id)
            .with_for_update()
        )
    ).one_or_none()


async def get_document(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, document_id: uuid.UUID
) -> DocumentSummary | None:
    """One document's metadata, scoped by BOTH room and id. No locks, no content."""
    async with sessionmaker() as db:
        row = (
            await db.execute(_joined().where(_DOCS.c.id == document_id, _DOCS.c.room_id == room_id))
        ).one_or_none()
    return _summary(row) if row is not None else None


async def list_documents(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[DocumentSummary], str | None]:
    """Newest first, keyset-paginated on (created_at, id); NEVER selects content.

        SELECT <metadata columns> FROM documents JOIN rooms ON ... WHERE room_id = :room
          [AND (created_at, id) < (:ts, :id)] ORDER BY created_at DESC, id DESC LIMIT :limit + 1

    Uses ix_documents_room_id_created_at_id; limit + 1 rows reveal whether another page exists.
    """
    after = decode_cursor(cursor)
    query = (
        _joined()
        .where(_DOCS.c.room_id == room_id)
        .order_by(_DOCS.c.created_at.desc(), _DOCS.c.id.desc())
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(tuple_(_DOCS.c.created_at, _DOCS.c.id) < tuple_(*after))
    async with sessionmaker() as db:
        rows = (await db.execute(query)).all()
    page, extra = rows[:limit], rows[limit:]
    items = [_summary(r) for r in page]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if extra else None
    return items, next_cursor


async def get_selection(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID
) -> Selection:
    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(_ROOMS.c.active_document_id, _ROOMS.c.active_document_version).where(
                    _ROOMS.c.id == room_id
                )
            )
        ).one()
    return Selection(row.active_document_id, row.active_document_version)


async def _activate(
    db: AsyncSession, locked, room_id: uuid.UUID, document_id: uuid.UUID, actor_id: uuid.UUID
) -> int:
    """Make `document_id` the room's active document and record active_document_changed.

    UPDATE rooms SET active_document_id = :d, active_document_version = active_document_version + 1,
    updated_at = updated_at: setting updated_at to itself stops the ORM-level onupdate default, so
    the selection changes neither rooms.version nor rooms.updated_at (it is not a rename or an
    access change). Returns the new selection version.
    """
    selection_version = (
        await db.execute(
            update(_ROOMS)
            .where(_ROOMS.c.id == room_id)
            .values(
                active_document_id=document_id,
                active_document_version=_ROOMS.c.active_document_version + 1,
                updated_at=_ROOMS.c.updated_at,
            )
            .returning(_ROOMS.c.active_document_version)
        )
    ).scalar_one()
    await record_room_event(
        db,
        locked,
        event_type="active_document_changed",
        resource_type="document",
        resource_id=document_id,
        resource_version=None,
        actor_id=actor_id,
        payload={"document_id": str(document_id), "selection_version": selection_version},
    )
    return selection_version


# ---- create -------------------------------------------------------------------------------------


async def create_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    title: str,
    template_key: str,
    client_request_id: uuid.UUID,
    *,
    content: dict | None = None,
    seed_format: str = "interchange",
    extra_fingerprint: str = "",
    before_insert=None,
    after_insert=None,
) -> CreateResult:
    """Owner-only. Creates the document, makes it the room's active document and records
    document_created then active_document_changed, ALL in one transaction. Or answers a retry.

    Before the lock (no database): build the template (a fresh UUID per node), validate it with the
    same validator as an editor save, compute the request fingerprint. Then, after the room lock:

      1. recheck the actor is STILL an active owner (404 / 403);
      2. look up (room, actor, client_request_id): found + same fingerprint -> return it unchanged
         (200: no write, no event, NO re-activation, even if another document is active now);
         found + different fingerprint -> 409 document_request_id_reused;
      3. not found: enforce the per-room document cap (409 document_limit_reached);
      4. INSERT the document (version 1, clock_timestamp()), activate it, record both events.

    If the INSERT raises a unique violation on uq_documents_room_creator_request (it cannot while
    the room lock serializes writers; the constraint is the final safeguard), the transaction rolls
    back and the whole thing re-runs once, which then answers as a retry. Permission is checked
    BEFORE the existing row is returned, so a demoted owner cannot use a retry to read a document.
    """
    # An AI draft brings its own already-validated content (validated Tiptap JSON, seed_format
    # "tiptap"); `before_insert` / `after_insert` run INSIDE the same transaction (the proposal is
    # checked first, and linked and marked in the same commit that creates the document).
    seed_content = (
        content
        if content is not None
        else template_document(template_key, limits_from_settings(settings)).content
    )
    fingerprint = compute_create_fingerprint(settings, title, template_key, extra_fingerprint)
    document_id: uuid.UUID
    created: bool
    for attempt in (1, 2):
        try:
            async with sessionmaker() as db, db.begin():
                locked, _role = await lock_room_and_actor(db, room_id, actor_id, owner_only=True)
                existing = (
                    await db.execute(
                        select(_DOCS.c.id, _DOCS.c.request_fingerprint).where(
                            _DOCS.c.room_id == room_id,
                            _DOCS.c.created_by == actor_id,
                            _DOCS.c.client_request_id == client_request_id,
                        )
                    )
                ).one_or_none()
                if existing is not None:
                    if not hmac.compare_digest(bytes(existing.request_fingerprint), fingerprint):
                        raise document_request_id_reused()
                    document_id, created = existing.id, False
                else:
                    if before_insert is not None:
                        await before_insert(db, locked)
                    count = (
                        await db.execute(
                            select(func.count())
                            .select_from(_DOCS)
                            .where(_DOCS.c.room_id == room_id)
                        )
                    ).scalar_one()
                    if count >= settings.max_documents_per_room:
                        raise document_limit_reached()
                    document_id = (
                        await db.execute(
                            insert(_DOCS)
                            .values(
                                room_id=room_id,
                                created_by=actor_id,
                                title=title,
                                template_key=template_key,
                                schema_version=1,
                                seed_format=seed_format,
                                content=seed_content,
                                content_hash=content_hash(seed_content),
                                client_request_id=client_request_id,
                                request_fingerprint=fingerprint,
                                created_at=func.clock_timestamp(),
                                updated_at=func.clock_timestamp(),
                                content_updated_at=func.clock_timestamp(),
                            )
                            .returning(_DOCS.c.id)
                        )
                    ).scalar_one()
                    await record_room_event(
                        db,
                        locked,
                        event_type="document_created",
                        resource_type="document",
                        resource_id=document_id,
                        resource_version=None,
                        actor_id=actor_id,
                        payload={"document_id": str(document_id)},
                    )
                    await _activate(db, locked, room_id, document_id, actor_id)
                    if after_insert is not None:
                        await after_insert(db, locked, document_id)
                    created = True
            break
        except IntegrityError as exc:
            if attempt == 2 or db_error_details(exc) != (UNIQUE_VIOLATION, REQUEST_ID_CONSTRAINT):
                raise
            log.info("duplicate document create raced past the lookup; retrying as a retry")

    view = await get_document(sessionmaker, room_id, document_id)
    if view is None:  # cannot happen: documents are never deleted
        raise document_not_found()
    if created:
        log_document_event("document_created", room_id, document_id, actor_id)
    return CreateResult(view, created)


# ---- rename and selection -----------------------------------------------------------------------


async def rename_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    document_id: uuid.UUID,
    title: str,
) -> DocumentSummary:
    """Owner-only. A same-title rename changes nothing (no version, no timestamp, no event).
    Otherwise metadata_version + 1 and updated_at move; content_version and content_updated_at do
    NOT (a rename is not a content change, so it never conflicts with a save)."""
    changed = False
    async with sessionmaker() as db, db.begin():
        locked, _role = await lock_room_and_actor(db, room_id, actor_id, owner_only=True)
        doc = await _lock_document(db, room_id, document_id)
        if doc is None:
            raise document_not_found()
        if doc.title != title:
            await db.execute(
                update(_DOCS)
                .where(_DOCS.c.id == document_id)
                .values(
                    title=title,
                    metadata_version=_DOCS.c.metadata_version + 1,
                    updated_at=func.clock_timestamp(),
                )
            )
            await record_room_event(
                db,
                locked,
                event_type="document_renamed",
                resource_type="document",
                resource_id=document_id,
                resource_version=None,
                actor_id=actor_id,
                payload={"document_id": str(document_id)},
            )
            changed = True
    if changed:
        log_document_event("document_renamed", room_id, document_id, actor_id)
    view = await get_document(sessionmaker, room_id, document_id)
    if view is None:
        raise document_not_found()
    return view


async def set_active_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    document_id: uuid.UUID,
    expected_selection_version: int | None,
) -> Selection:
    """Owner-only. After the room lock: the document must be in THIS room (404); a stale
    expected_selection_version is 409 selection_conflict; choosing the already-active document is a
    no-op (no version bump, no event); otherwise update and record active_document_changed."""
    changed = False
    async with sessionmaker() as db, db.begin():
        locked, _role = await lock_room_and_actor(db, room_id, actor_id, owner_only=True)
        doc = await _lock_document(db, room_id, document_id)
        if doc is None:
            raise document_not_found()
        # lock_room() selected the whole rooms row FOR NO KEY UPDATE, so this is current
        current = locked.row
        if (
            expected_selection_version is not None
            and expected_selection_version != current.active_document_version
        ):
            raise selection_conflict(current.active_document_version)
        if current.active_document_id == document_id:
            return Selection(current.active_document_id, current.active_document_version)
        version = await _activate(db, locked, room_id, document_id, actor_id)
        changed = True
    if changed:
        log_document_event("active_document_changed", room_id, document_id, actor_id)
    return Selection(document_id, version)


# ---- asset references (maintained from validated collaboration snapshots) -----------------------


async def _sync_asset_refs(
    db: AsyncSession, room_id: uuid.UUID, document_id: uuid.UUID, wanted: set[uuid.UUID]
) -> None:
    """Make document_asset_refs equal `wanted` and move the assets' lifecycle with it.

    Newly referenced assets become 'attached' (a pending one gets attached_at) and lose any
    unreferenced_since. Assets this document no longer references get unreferenced_since = now,
    but only if NO document references them any more; they are deleted by the cleanup command
    after the grace period, never here (so a client undo shortly after a save still works).
    """
    current = set(
        (await db.execute(select(_REFS.c.asset_id).where(_REFS.c.document_id == document_id)))
        .scalars()
        .all()
    )
    added, removed = wanted - current, current - wanted
    if added:
        await db.execute(
            insert(_REFS),
            [
                {"document_id": document_id, "asset_id": a, "room_id": room_id}
                for a in sorted(added)
            ],
        )
        await db.execute(
            update(_ASSETS)
            .where(_ASSETS.c.room_id == room_id, _ASSETS.c.id.in_(added))
            .values(
                state="attached",
                attached_at=func.coalesce(_ASSETS.c.attached_at, func.clock_timestamp()),
                unreferenced_since=None,
            )
        )
    if removed:
        await db.execute(
            delete(_REFS).where(_REFS.c.document_id == document_id, _REFS.c.asset_id.in_(removed))
        )
        await db.execute(
            update(_ASSETS)
            .where(
                _ASSETS.c.room_id == room_id,
                _ASSETS.c.id.in_(removed),
                _ASSETS.c.state == "attached",
                ~exists().where(_REFS.c.asset_id == _ASSETS.c.id),
            )
            .values(unreferenced_since=func.clock_timestamp())
        )


sync_asset_refs = _sync_asset_refs  # public name for the collaboration snapshot path
