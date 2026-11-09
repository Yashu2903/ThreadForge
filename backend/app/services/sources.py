"""Sources: files that AI requests can use as evidence (AI phase 1, checkpoint 2).

The ONLY module that writes `sources`, `source_versions` and `source_uploads`.

The life of a device upload (three HTTP calls, because the bytes can be large and must be counted
as they arrive, and because inspecting is a separate, slower step):

  1. INTENT    POST .../source-uploads        row in state `intent` (name, declared size, idempotency
                                              key). Rate limit, quotas and the open-upload cap are
                                              checked here, under the room lock.
  2. BYTES     PUT  .../content               the body is streamed to a temp file, counted, hashed,
                                              signature-checked, moved to storage under an opaque
                                              key; row becomes `uploaded`.
  3. FINALIZE  POST .../finalize              the sandboxed child inspects the file (cheap: validity,
                                              pages, text layer, scans, images). Then, under the room
                                              lock: quotas again with the REAL size, a new source (or
                                              a new version of an existing source) is inserted and
                                              the row becomes `finalized`.

Nothing here talks to an AI provider. Uploading never analyzes anything: `analysis_state` stays
`not_analyzed` until checkpoint 3's worker is asked to (docs/ai.md section 9).

Lock order everywhere: the room row first (membership and role are re-read under it), then the
`sources` row, then the upload row. File work (storage reads/writes/deletes, the child process)
never happens inside a transaction. A crash between "file stored" and "row updated" leaves an
orphan file that `python -m app.commands.cleanup_ai` removes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import (
    AppError,
    file_type_error,
    insufficient_role,
    invalid_source,
    payload_too_large,
    quota_exceeded,
    rate_limited,
    source_not_found,
    source_unavailable,
    source_upload_id_reused,
    source_upload_not_found,
    source_upload_not_open,
)
from app.logging_config import APP_LOGGER, request_id_var
from app.models import Source, SourceUpload, SourceVersion
from app.services.room_events import record_room_event
from app.services.rooms import lock_room_and_actor
from app.sources import files
from app.sources.sandbox import InspectionRejected, inspect_file
from app.storage import Storage

log = logging.getLogger(f"{APP_LOGGER}.sources")
_SRC = Source.__table__
_VER = SourceVersion.__table__
_UP = SourceUpload.__table__
EDIT_ROLES = ("owner", "editor")
OPEN_STATES = ("intent", "uploaded")
FINGERPRINT_VERSION = b"source-upload-v1"
NAMESPACE = "sources"


# ---- views ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionView:
    id: uuid.UUID
    source_id: uuid.UUID
    version_no: int
    original_name: str
    kind: str
    media_type: str
    byte_size: int
    sha256: str  # hex
    analysis_state: str
    page_count: int | None
    text_page_count: int | None
    likely_scanned_pages: int | None
    image_count: int
    text_chars: int
    error_code: str | None
    created_by: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class SourceView:
    id: uuid.UUID
    room_id: uuid.UUID
    display_name: str
    state: str
    created_by: uuid.UUID
    created_at: datetime
    current_version: VersionView | None
    version_count: int
    versions: tuple[VersionView, ...] = ()  # filled by get_source only


@dataclass(frozen=True)
class UploadView:
    id: uuid.UUID
    room_id: uuid.UUID
    state: str
    original_name: str
    kind: str
    declared_size: int
    byte_size: int | None
    sha256: str | None
    error_code: str | None
    source_id: uuid.UUID | None
    result_version_id: uuid.UUID | None
    created_at: datetime
    expires_at: datetime


def _version_view(row) -> VersionView:
    return VersionView(
        id=row.id,
        source_id=row.source_id,
        version_no=row.version_no,
        original_name=row.original_name,
        kind=row.kind,
        media_type=row.media_type,
        byte_size=row.byte_size,
        sha256=bytes(row.sha256).hex(),
        analysis_state=row.analysis_state,
        page_count=row.page_count,
        text_page_count=row.text_page_count,
        likely_scanned_pages=row.likely_scanned_pages,
        image_count=row.image_count,
        text_chars=row.text_chars,
        error_code=row.error_code,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _upload_view(row) -> UploadView:
    return UploadView(
        id=row.id,
        room_id=row.room_id,
        state=row.state,
        original_name=row.original_name,
        kind=row.kind,
        declared_size=row.declared_size,
        byte_size=row.byte_size,
        sha256=bytes(row.sha256).hex() if row.sha256 is not None else None,
        error_code=row.error_code,
        source_id=row.source_id,
        result_version_id=row.result_version_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def rejection_error(code: str, message: str) -> AppError:
    """Map a stable rejection code to its HTTP error (type problems are 415, the rest 422)."""
    if code in ("type_mismatch", "unsupported_media_type"):
        return file_type_error(code, message)
    return invalid_source(code, message)


# ---- idempotency ------------------------------------------------------------------------------------


def compute_fingerprint(
    settings: Settings,
    client_request_id: uuid.UUID,
    name: str,
    declared_type: str | None,
    declared_size: int,
    source_id: uuid.UUID | None,
) -> bytes:
    """HMAC-SHA256 (keyed, like the message fingerprint) over the intent's fields. Same
    client_request_id + same fingerprint = the same upload; same id + different fields = 409."""
    canonical = json.dumps(
        {
            "client_request_id": str(client_request_id),
            "name": name,
            "type": declared_type or "",
            "size": declared_size,
            "source_id": str(source_id) if source_id else "",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    key = settings.message_fingerprint_secret.get_secret_value().encode("utf-8")
    return hmac.new(key, FINGERPRINT_VERSION + b"\n" + canonical, hashlib.sha256).digest()


# ---- quotas -----------------------------------------------------------------------------------------


async def _check_quotas(
    db: AsyncSession,
    settings: Settings,
    room_id: uuid.UUID,
    *,
    new_source: bool,
    add_bytes: int,
    exclude_upload: uuid.UUID | None = None,
) -> None:
    """Room limits. Counted: active sources, plus new-source uploads that are in flight (so two
    parallel uploads cannot both take the last slot); live version bytes, plus the declared bytes of
    in-flight uploads. Removed sources, finished/failed/cancelled/expired uploads count for nothing."""
    in_flight = (
        _UP.c.room_id == room_id,
        _UP.c.state.in_(OPEN_STATES),
        _UP.c.expires_at > func.clock_timestamp(),
    )
    not_me = (_UP.c.id != exclude_upload,) if exclude_upload else ()
    if new_source:
        active = (
            await db.execute(
                select(func.count()).where(_SRC.c.room_id == room_id, _SRC.c.state == "active")
            )
        ).scalar_one()
        pending = (
            await db.execute(
                select(func.count()).where(*in_flight, *not_me, _UP.c.source_id.is_(None))
            )
        ).scalar_one()
        if active + pending >= settings.source_max_per_room:
            raise quota_exceeded("This room has reached its maximum number of sources.")
    live = (
        await db.execute(
            select(func.coalesce(func.sum(_VER.c.byte_size), 0)).where(
                _VER.c.room_id == room_id, _VER.c.analysis_state != "removed"
            )
        )
    ).scalar_one()
    reserved = (
        await db.execute(
            select(func.coalesce(func.sum(_UP.c.declared_size), 0)).where(*in_flight, *not_me)
        )
    ).scalar_one()
    if live + reserved + add_bytes > settings.source_room_quota_bytes:
        raise quota_exceeded("This room has used up its source storage.")


# ---- step 1: intent ---------------------------------------------------------------------------------


async def create_upload_intent(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    *,
    client_request_id: uuid.UUID,
    filename: str,
    declared_type: str | None,
    declared_size: int,
    source_id: uuid.UUID | None,
) -> tuple[UploadView, bool]:
    """Owner or editor. Returns (upload, created). A retry with the same client_request_id and the
    same fields returns the existing upload (created=False) and consumes nothing."""
    name = files.sanitize_name(filename)
    try:
        kind = files.classify(name, declared_type)
    except files.UnsupportedFile as exc:
        raise file_type_error(exc.code, exc.message) from None
    if declared_size < 1:
        raise invalid_source("empty", "The file is empty.")
    if declared_size > settings.source_max_upload_bytes:
        raise payload_too_large()
    fingerprint = compute_fingerprint(
        settings, client_request_id, name, declared_type, declared_size, source_id
    )
    async with sessionmaker() as db, db.begin():
        _locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        if role not in EDIT_ROLES:
            raise insufficient_role()
        existing = (
            await db.execute(
                select(_UP).where(
                    _UP.c.room_id == room_id,
                    _UP.c.created_by == actor_id,
                    _UP.c.client_request_id == client_request_id,
                )
            )
        ).one_or_none()
        if existing is not None:
            if not hmac.compare_digest(bytes(existing.request_fingerprint), fingerprint):
                raise source_upload_id_reused()
            return _upload_view(existing), False

        if source_id is not None:
            target = (
                await db.execute(
                    select(_SRC.c.state).where(_SRC.c.id == source_id, _SRC.c.room_id == room_id)
                )
            ).scalar_one_or_none()
            if target != "active":
                raise source_not_found()

        # per-user upload rate: intents created in the last minute (a refused intent consumes nothing)
        recent = (
            await db.execute(
                select(func.count(), func.min(_UP.c.created_at)).where(
                    _UP.c.room_id == room_id,
                    _UP.c.created_by == actor_id,
                    _UP.c.created_at
                    > func.clock_timestamp() - func.make_interval(0, 0, 0, 0, 0, 1),
                )
            )
        ).one()
        if recent[0] >= settings.source_uploads_per_minute:
            wait = (
                await db.execute(
                    select(
                        func.extract(
                            "epoch",
                            recent[1]
                            + func.make_interval(0, 0, 0, 0, 0, 1)
                            - func.clock_timestamp(),
                        )
                    )
                )
            ).scalar_one()
            raise rate_limited(max(1, math.ceil(wait)), "Too many uploads. Try again shortly.")
        open_count = (
            await db.execute(
                select(func.count()).where(
                    _UP.c.room_id == room_id,
                    _UP.c.created_by == actor_id,
                    _UP.c.state.in_(OPEN_STATES),
                    _UP.c.expires_at > func.clock_timestamp(),
                )
            )
        ).scalar_one()
        if open_count >= settings.source_max_open_uploads_per_user:
            raise AppError(
                409,
                "too_many_open_uploads",
                "Finish or cancel your other uploads in this room first.",
            )
        await _check_quotas(
            db, settings, room_id, new_source=source_id is None, add_bytes=declared_size
        )
        row = (
            await db.execute(
                insert(_UP)
                .values(
                    room_id=room_id,
                    created_by=actor_id,
                    client_request_id=client_request_id,
                    request_fingerprint=fingerprint,
                    source_id=source_id,
                    original_name=name,
                    kind=kind,
                    declared_media_type=(declared_type or None),
                    declared_size=declared_size,
                    state="intent",
                    created_at=func.clock_timestamp(),
                    expires_at=func.clock_timestamp()
                    + func.make_interval(0, 0, 0, 0, 0, settings.source_upload_ttl_minutes),
                )
                .returning(*_UP.c)
            )
        ).one()
    log.info(
        "action=source_upload_started room_id=%s upload_id=%s actor_id=%s kind=%s request_id=%s",
        room_id,
        row.id,
        actor_id,
        kind,
        request_id_var.get(),
    )
    return _upload_view(row), True


# ---- step 2: bytes ----------------------------------------------------------------------------------


async def _load_upload(db: AsyncSession, room_id, actor_id, upload_id, *, lock: bool = False):
    stmt = select(_UP, (_UP.c.expires_at <= func.clock_timestamp()).label("is_expired")).where(
        _UP.c.id == upload_id, _UP.c.room_id == room_id, _UP.c.created_by == actor_id
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise source_upload_not_found()  # also for another user's upload: indistinguishable
    return row


async def get_upload(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    actor_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> UploadView:
    async with sessionmaker() as db:
        row = await _load_upload(db, room_id, actor_id, upload_id)
    view = _upload_view(row)
    if row.is_expired and row.state in OPEN_STATES:
        return dataclasses.replace(view, state="expired")  # cleanup has not run yet
    return view


async def precheck_content(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    actor_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> tuple[UploadView, bool]:
    """Before the body is read: (view, accepts_bytes). Already `uploaded`/`finalized` is returned
    as is (a retried PUT whose first response was lost); other states cannot take bytes."""
    async with sessionmaker() as db:
        row = await _load_upload(db, room_id, actor_id, upload_id)
    if row.state in ("uploaded", "finalized"):
        return _upload_view(row), False
    if row.state != "intent" or row.is_expired:
        raise source_upload_not_open("expired" if row.is_expired else row.state)
    return _upload_view(row), True


async def record_bytes(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    upload_id: uuid.UUID,
    *,
    storage_key: str,
    byte_size: int,
    sha256: bytes,
) -> UploadView:
    """After the file is in storage: `intent` -> `uploaded`, under the room lock. If this raises the
    caller deletes the stored file."""
    async with sessionmaker() as db, db.begin():
        locked_room, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        if role not in EDIT_ROLES:
            raise insufficient_role()
        row = await _load_upload(db, room_id, actor_id, upload_id, lock=True)
        if row.state != "intent" or row.is_expired:
            raise source_upload_not_open("expired" if row.is_expired else row.state)
        updated = (
            await db.execute(
                update(_UP)
                .where(_UP.c.id == upload_id)
                .values(
                    state="uploaded",
                    storage_key=storage_key,
                    byte_size=byte_size,
                    sha256=sha256,
                    uploaded_at=func.clock_timestamp(),
                )
                .returning(*_UP.c)
            )
        ).one()
    return _upload_view(updated)


async def end_upload(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: Storage,
    room_id: uuid.UUID,
    upload_id: uuid.UUID,
    *,
    state: str,
    error_code: str | None = None,
) -> None:
    """Move an OPEN upload to failed/cancelled and delete its bytes. Safe to call twice."""
    async with sessionmaker() as db, db.begin():
        await db.execute(
            update(_UP)
            .where(_UP.c.id == upload_id, _UP.c.room_id == room_id, _UP.c.state.in_(OPEN_STATES))
            .values(state=state, error_code=error_code, finished_at=func.clock_timestamp())
        )
    await release_upload_bytes(sessionmaker, storage, upload_id)


async def release_upload_bytes(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, upload_id: uuid.UUID
) -> None:
    """Delete the file of a TERMINAL (failed/cancelled/expired) upload, then forget its key."""
    async with sessionmaker() as db:
        key = (
            await db.execute(
                select(_UP.c.storage_key).where(
                    _UP.c.id == upload_id, _UP.c.state.in_(("failed", "cancelled", "expired"))
                )
            )
        ).scalar_one_or_none()
    if key is None:
        return
    try:
        storage.delete(NAMESPACE, key)
    except OSError:
        log.warning("action=source_file_delete_failed upload_id=%s", upload_id)
        return  # the key stays; cleanup_ai retries
    async with sessionmaker() as db, db.begin():
        await db.execute(
            update(_UP)
            .where(_UP.c.id == upload_id, _UP.c.storage_key == key)
            .values(storage_key=None)
        )


async def cancel_upload(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: Storage,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> UploadView:
    """The creator abandons an unfinished upload; bytes go now. Finished uploads are unaffected."""
    async with sessionmaker() as db, db.begin():
        _locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        if role not in EDIT_ROLES:
            raise insufficient_role()
        row = await _load_upload(db, room_id, actor_id, upload_id, lock=True)
        if row.state in OPEN_STATES:
            await db.execute(
                update(_UP)
                .where(_UP.c.id == upload_id)
                .values(state="cancelled", finished_at=func.clock_timestamp())
            )
    await release_upload_bytes(sessionmaker, storage, upload_id)
    return await get_upload(sessionmaker, room_id, actor_id, upload_id)


# ---- step 3: finalize -------------------------------------------------------------------------------


async def finalize_upload(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    slots: asyncio.Semaphore,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> SourceView:
    """Inspect the stored file in the sandbox, then create the source/version. Idempotent: a
    finalized upload returns the same source again."""
    async with sessionmaker() as db:
        upload = await _load_upload(db, room_id, actor_id, upload_id)
    if upload.state == "finalized":
        return await _source_of_version(sessionmaker, room_id, upload.result_version_id)
    if upload.state != "uploaded" or upload.is_expired:
        raise source_upload_not_open("expired" if upload.is_expired else upload.state)

    path = storage.path_for(NAMESPACE, upload.storage_key)
    try:
        async with slots:
            inspection = await inspect_file(path, upload.kind, settings, storage.tmp_dir)
    except InspectionRejected as exc:
        await end_upload(
            sessionmaker, storage, room_id, upload_id, state="failed", error_code=exc.code
        )
        log.info(
            "action=source_rejected room_id=%s upload_id=%s code=%s request_id=%s",
            room_id,
            upload_id,
            exc.code,
            request_id_var.get(),
        )
        raise rejection_error(exc.code, exc.message) from None

    async with sessionmaker() as db, db.begin():
        locked_room, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        if role not in EDIT_ROLES:
            raise insufficient_role()
        row = await _load_upload(db, room_id, actor_id, upload_id, lock=True)
        if row.state == "finalized":  # a concurrent finalize won
            version_id = row.result_version_id
            created = None
        else:
            if row.state != "uploaded" or row.is_expired:
                raise source_upload_not_open("expired" if row.is_expired else row.state)
            await _check_quotas(
                db,
                settings,
                room_id,
                new_source=row.source_id is None,
                add_bytes=row.byte_size,
                exclude_upload=row.id,
            )
            if row.source_id is None:
                source_id = (
                    await db.execute(
                        insert(_SRC)
                        .values(
                            room_id=room_id,
                            created_by=actor_id,
                            display_name=row.original_name[:200],
                            created_at=func.clock_timestamp(),
                        )
                        .returning(_SRC.c.id)
                    )
                ).scalar_one()
                version_no = 1
            else:
                locked_source = (
                    await db.execute(
                        select(_SRC.c.state)
                        .where(_SRC.c.id == row.source_id, _SRC.c.room_id == room_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if locked_source != "active":
                    raise source_not_found()
                source_id = row.source_id
                version_no = (
                    await db.execute(
                        select(func.coalesce(func.max(_VER.c.version_no), 0) + 1).where(
                            _VER.c.source_id == source_id
                        )
                    )
                ).scalar_one()
            version_id = (
                await db.execute(
                    insert(_VER)
                    .values(
                        source_id=source_id,
                        room_id=room_id,
                        version_no=version_no,
                        original_name=row.original_name,
                        kind=row.kind,
                        media_type=files.MEDIA_TYPE_BY_KIND[row.kind],
                        byte_size=row.byte_size,
                        sha256=bytes(row.sha256),
                        storage_key=row.storage_key,
                        analysis_state="not_analyzed",
                        page_count=inspection.page_count,
                        text_page_count=inspection.text_page_count,
                        likely_scanned_pages=inspection.likely_scanned_pages,
                        image_count=inspection.image_count,
                        text_chars=inspection.text_chars,
                        created_by=actor_id,
                        created_at=func.clock_timestamp(),
                    )
                    .returning(_VER.c.id)
                )
            ).scalar_one()
            await db.execute(
                update(_SRC).where(_SRC.c.id == source_id).values(current_version_id=version_id)
            )
            await db.execute(
                update(_UP)
                .where(_UP.c.id == upload_id)
                .values(
                    state="finalized",
                    result_version_id=version_id,
                    storage_key=None,  # the version owns the file now
                    finished_at=func.clock_timestamp(),
                )
            )
            created = (source_id, version_id, version_no)
            await emit_source_event(db, locked_room, source_id, version_id, "created", actor_id)
    if created:
        log.info(
            "action=source_version_created room_id=%s source_id=%s version_id=%s version_no=%s "
            "actor_id=%s request_id=%s",
            room_id,
            created[0],
            created[1],
            created[2],
            actor_id,
            request_id_var.get(),
        )
    return await _source_of_version(sessionmaker, room_id, version_id)


async def _source_of_version(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, version_id: uuid.UUID
) -> SourceView:
    async with sessionmaker() as db:
        source_id = (
            await db.execute(
                select(_VER.c.source_id).where(_VER.c.id == version_id, _VER.c.room_id == room_id)
            )
        ).scalar_one()
    view = await get_source(sessionmaker, room_id, source_id)
    if view is None:  # removed in the meantime
        raise source_not_found()
    return view


# ---- reads ------------------------------------------------------------------------------------------


async def list_sources(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID
) -> list[SourceView]:
    """Active sources with their current version. Any member may list."""
    count = (
        select(func.count()).where(_VER.c.source_id == _SRC.c.id).correlate(_SRC).scalar_subquery()
    )
    stmt = (
        select(_SRC, count.label("version_count"), *[c.label(f"v_{c.name}") for c in _VER.c])
        .select_from(
            _SRC.outerjoin(
                _VER, (_VER.c.id == _SRC.c.current_version_id) & (_VER.c.room_id == _SRC.c.room_id)
            )
        )
        .where(_SRC.c.room_id == room_id, _SRC.c.state == "active")
        .order_by(_SRC.c.created_at, _SRC.c.id)
    )
    async with sessionmaker() as db:
        rows = (await db.execute(stmt)).all()
    out = []
    for r in rows:
        current = _version_view(_Prefixed(r, "v_")) if r.v_id is not None else None
        out.append(
            SourceView(
                id=r.id,
                room_id=r.room_id,
                display_name=r.display_name,
                state=r.state,
                created_by=r.created_by,
                created_at=r.created_at,
                current_version=current,
                version_count=r.version_count,
            )
        )
    return out


async def room_bytes_used(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID
) -> int:
    """Bytes held by the room's live (not removed) versions."""
    async with sessionmaker() as db:
        return (
            await db.execute(
                select(func.coalesce(func.sum(_VER.c.byte_size), 0)).where(
                    _VER.c.room_id == room_id, _VER.c.analysis_state != "removed"
                )
            )
        ).scalar_one()


class _Prefixed:
    """Read `row.v_<name>` as `.<name>` so one version mapper serves joined and plain rows."""

    def __init__(self, row, prefix: str) -> None:
        self._row, self._prefix = row, prefix

    def __getattr__(self, name: str):
        return getattr(self._row, self._prefix + name)


async def get_source(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, source_id: uuid.UUID
) -> SourceView | None:
    """An active source with ALL its versions (newest first), scoped by room and id."""
    async with sessionmaker() as db:
        src = (
            await db.execute(
                select(_SRC).where(
                    _SRC.c.id == source_id, _SRC.c.room_id == room_id, _SRC.c.state == "active"
                )
            )
        ).one_or_none()
        if src is None:
            return None
        versions = (
            await db.execute(
                select(_VER)
                .where(_VER.c.source_id == source_id, _VER.c.room_id == room_id)
                .order_by(_VER.c.version_no.desc())
            )
        ).all()
    views = tuple(_version_view(v) for v in versions)
    current = next((v for v in views if v.id == src.current_version_id), None)
    return SourceView(
        id=src.id,
        room_id=src.room_id,
        display_name=src.display_name,
        state=src.state,
        created_by=src.created_by,
        created_at=src.created_at,
        current_version=current,
        version_count=len(views),
        versions=views,
    )


@dataclass(frozen=True)
class StoredFile:
    version: VersionView
    storage_key: str


async def get_version_file(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    source_id: uuid.UUID,
    version_id: uuid.UUID,
) -> StoredFile | None:
    """The stored file of a version of an ACTIVE source, scoped by room, source and version."""
    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(_VER)
                .join(_SRC, (_SRC.c.id == _VER.c.source_id) & (_SRC.c.room_id == _VER.c.room_id))
                .where(
                    _VER.c.id == version_id,
                    _VER.c.source_id == source_id,
                    _VER.c.room_id == room_id,
                    _VER.c.analysis_state != "removed",
                    _SRC.c.state == "active",
                )
            )
        ).one_or_none()
    if row is None or row.storage_key is None:
        return None
    return StoredFile(_version_view(row), row.storage_key)


async def resolve_versions(
    db: AsyncSession, room_id: uuid.UUID, version_ids: list[uuid.UUID]
) -> list[VersionView]:
    """For request admission (checkpoint 4, inside the room-locked transaction): every id must be a
    version of an ACTIVE source in THIS room and not removed, else source_unavailable naming the
    offenders. Duplicates collapse; order follows the input."""
    wanted = list(dict.fromkeys(version_ids))
    rows = (
        await db.execute(
            select(_VER)
            .join(_SRC, (_SRC.c.id == _VER.c.source_id) & (_SRC.c.room_id == _VER.c.room_id))
            .where(
                _VER.c.room_id == room_id,
                _VER.c.id.in_(wanted),
                _VER.c.analysis_state != "removed",
                _SRC.c.state == "active",
            )
        )
    ).all()
    found = {r.id: _version_view(r) for r in rows}
    missing = [str(v) for v in wanted if v not in found]
    if missing:
        raise source_unavailable(missing)
    return [found[v] for v in wanted]


async def emit_source_event(
    db: AsyncSession,
    locked,
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    state: str,
    actor_id: uuid.UUID,
) -> None:
    """`source_updated`: a new version, an analysis that reached ready or failed, a removal. Ids and
    the state only; the details come from the authorized endpoints. Needs the room lock."""
    await record_room_event(
        db,
        locked,
        event_type="source_updated",
        resource_type="source",
        resource_id=source_id,
        resource_version=None,
        actor_id=actor_id,
        payload={"source_id": str(source_id), "source_version_id": str(version_id), "state": state},
    )


# ---- removal ----------------------------------------------------------------------------------------


async def remove_source(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: Storage,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    source_id: uuid.UUID,
) -> int:
    """Owner: any source. Editor: only a source they created. Viewer: never.

    One transaction (under the room lock) marks the source and every version `removed`; the file
    names stay on the rows until the files are really gone. Then, outside the transaction, the
    original bytes are deleted and the names cleared. If deleting fails, `cleanup_ai` finishes it
    (rows with state `removed` and a storage key). Returns the number of files deleted.

    Checkpoint 3 adds chunks, embeddings and figure images to this same removal; checkpoint 4 adds
    redaction of stored excerpts and manifests. Already-published chat text stays (docs/ai.md).
    """
    async with sessionmaker() as db, db.begin():
        locked_room, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        src = (
            await db.execute(
                select(_SRC)
                .where(_SRC.c.id == source_id, _SRC.c.room_id == room_id)
                .with_for_update()
            )
        ).one_or_none()
        if src is None or src.state != "active":
            raise source_not_found()
        if not (role == "owner" or (role == "editor" and src.created_by == actor_id)):
            raise insufficient_role()
        await db.execute(
            update(_SRC)
            .where(_SRC.c.id == source_id)
            .values(state="removed", removed_at=func.clock_timestamp(), removed_by=actor_id)
        )
        keys = (
            (
                await db.execute(
                    update(_VER)
                    .where(_VER.c.source_id == source_id, _VER.c.analysis_state != "removed")
                    .values(
                        analysis_state="removed",
                        removed_at=func.clock_timestamp(),
                        current_generation_id=None,
                    )
                    .returning(_VER.c.storage_key)
                )
            )
            .scalars()
            .all()
        )
        # Analysis data goes in the SAME transaction: jobs stop (a running worker finds its claim
        # gone), and every generation of these versions is deleted, which cascades to the chunks, the
        # vectors and the figure rows. Only the figure IMAGE FILES are deleted after the commit.
        figure_keys = await _remove_analysis(db, source_id)
        # published answers keep their text but lose the excerpts and quotes taken from these files;
        # manifests lose the stored passages (citations become 'source removed' stubs)
        from app.services import ai_requests  # late import: that module imports this one

        removed_ids = list(
            (await db.execute(select(_VER.c.id).where(_VER.c.source_id == source_id)))
            .scalars()
            .all()
        )
        await ai_requests.redact_for_removed_versions(db, removed_ids)
        if removed_ids:
            await emit_source_event(
                db, locked_room, source_id, removed_ids[-1], "removed", actor_id
            )
    log.info(
        "action=source_removed room_id=%s source_id=%s actor_id=%s files=%s request_id=%s",
        room_id,
        source_id,
        actor_id,
        len(keys),
        request_id_var.get(),
    )
    await _delete_figure_files(storage, figure_keys)
    return await delete_removed_files(sessionmaker, storage, list(keys))


async def _remove_analysis(db: AsyncSession, source_id: uuid.UUID) -> list[str]:
    """Cancel the analysis jobs of the source's versions and delete their indexes. Returns the storage
    keys of figure images to delete once the transaction has committed."""
    version_ids = list(
        (await db.execute(select(_VER.c.id).where(_VER.c.source_id == source_id))).scalars().all()
    )
    if not version_ids:
        return []
    await db.execute(
        text(
            """UPDATE analysis_jobs SET state = 'cancelled', claim_token = NULL,
                   lease_expires_at = NULL, stage = NULL, finished_at = now()
                WHERE source_version_id = ANY(:v) AND state IN ('queued', 'running')"""
        ),
        {"v": version_ids},
    )
    figure_keys = list(
        (
            await db.execute(
                text(
                    "SELECT storage_key FROM figures WHERE source_version_id = ANY(:v) "
                    "AND storage_key IS NOT NULL"
                ),
                {"v": version_ids},
            )
        )
        .scalars()
        .all()
    )
    await db.execute(
        text("DELETE FROM index_generations WHERE source_version_id = ANY(:v)"), {"v": version_ids}
    )
    return figure_keys


async def _delete_figure_files(storage: Storage, keys: list[str]) -> None:
    for key in keys:
        try:
            storage.delete("figures", key)
        except OSError:
            log.warning("action=figure_file_delete_failed")  # cleanup_ai removes the orphan


async def delete_removed_files(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, keys: list[str | None]
) -> int:
    deleted = 0
    for key in keys:
        if key is None:
            continue
        try:
            storage.delete(NAMESPACE, key)
        except OSError:
            log.warning("action=source_file_delete_failed")  # cleanup_ai retries; the key stays
            continue
        deleted += 1
        async with sessionmaker() as db, db.begin():
            await db.execute(
                update(_VER)
                .where(_VER.c.storage_key == key, _VER.c.analysis_state == "removed")
                .values(storage_key=None)
            )
    return deleted


def file_path(storage: Storage, key: str) -> Path:
    return storage.path_for(NAMESPACE, key)
