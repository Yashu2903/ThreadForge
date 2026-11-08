"""Room assets (images): the ONLY module that inserts room_assets rows for uploads.

Lifecycle (the states live in room_assets.state; the transitions are in these places):
  pending   created here. It is attached by the first document save that references it
            (services/documents._sync_asset_refs), or expires after ASSET_PENDING_TTL_HOURS.
  attached  referenced by at least one document's current content. When the last reference goes it
            gets unreferenced_since and is expired ASSET_UNREFERENCED_GRACE_DAYS later (so a client
            undo shortly after a save still finds its image). Both expiries are done by
            `python -m app.commands.cleanup_documents`, which also deletes the bytes.
  expired   the bytes are gone; the row stays for audit. Removing an image from UNSAVED content
            deletes nothing.

Transaction pattern and lock order are the same as the other services: room lock first (membership
and role are rechecked under it), then the checks and the insert, one commit. No file is read or
written inside the transaction: the caller has already stored the re-encoded bytes under a fresh
opaque key; if this function refuses, the caller deletes that file.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.assets_pipeline import ProcessedImage
from app.config import Settings
from app.errors import insufficient_role, quota_exceeded, rate_limited
from app.logging_config import APP_LOGGER, request_id_var
from app.models import RoomAsset
from app.services.rooms import lock_room_and_actor

log = logging.getLogger(f"{APP_LOGGER}.assets")
_ASSETS = RoomAsset.__table__
EDIT_ROLES = ("owner", "editor")


@dataclass(frozen=True)
class AssetView:
    id: uuid.UUID
    room_id: uuid.UUID
    media_type: str
    byte_size: int
    width_px: int
    height_px: int
    sha256: str  # hex of the stored (re-encoded) bytes
    state: str
    storage_key: str  # internal: never put in a response
    created_at: datetime


def _view(row) -> AssetView:
    return AssetView(
        id=row.id,
        room_id=row.room_id,
        media_type=row.media_type,
        byte_size=row.byte_size,
        width_px=row.width_px,
        height_px=row.height_px,
        sha256=bytes(row.sha256).hex(),
        state=row.state,
        storage_key=row.storage_key,
        created_at=row.created_at,
    )


async def create_asset(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    processed: ProcessedImage,
    storage_key: str,
) -> AssetView:
    """Owner or editor. After the room lock: recheck role (404 / 403), enforce the upload rate
    (counted from rows this user created in the last minute, so a refused or rolled-back upload
    consumes nothing) and the room quotas (total bytes and asset count, expired assets excluded),
    then insert a PENDING asset. No room event: uploads are private until a save references them."""
    async with sessionmaker() as db, db.begin():
        _locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        if role not in EDIT_ROLES:
            raise insufficient_role()
        recent = (
            await db.execute(
                select(func.count(), func.min(_ASSETS.c.created_at)).where(
                    _ASSETS.c.room_id == room_id,
                    _ASSETS.c.uploaded_by == actor_id,
                    _ASSETS.c.created_at
                    > func.clock_timestamp() - func.make_interval(0, 0, 0, 0, 0, 1),
                )
            )
        ).one()
        if recent[0] >= settings.asset_uploads_per_minute:
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
            raise rate_limited(
                max(1, math.ceil(wait)), "Too many image uploads. Try again shortly."
            )
        count, total = (
            await db.execute(
                select(func.count(), func.coalesce(func.sum(_ASSETS.c.byte_size), 0)).where(
                    _ASSETS.c.room_id == room_id, _ASSETS.c.state != "expired"
                )
            )
        ).one()
        if count >= settings.asset_max_per_room:
            raise quota_exceeded("This room has reached its maximum number of images.")
        if total + processed.byte_size > settings.asset_room_quota_bytes:
            raise quota_exceeded("This room has used up its image storage.")
        row = (
            await db.execute(
                insert(_ASSETS)
                .values(
                    room_id=room_id,
                    uploaded_by=actor_id,
                    origin="editor_upload",
                    state="pending",
                    media_type=processed.media_type,
                    byte_size=processed.byte_size,
                    sha256=processed.sha256,
                    width_px=processed.width,
                    height_px=processed.height,
                    storage_key=storage_key,
                    created_at=func.clock_timestamp(),
                )
                .returning(*_ASSETS.c)
            )
        ).one()
    log.info(
        "action=asset_uploaded room_id=%s asset_id=%s actor_id=%s request_id=%s",
        room_id,
        row.id,
        actor_id,
        request_id_var.get(),
    )
    return _view(row)


async def get_asset(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, asset_id: uuid.UUID
) -> AssetView | None:
    """One asset, scoped by BOTH room and id (another room's asset id is simply not found)."""
    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(_ASSETS).where(_ASSETS.c.id == asset_id, _ASSETS.c.room_id == room_id)
            )
        ).one_or_none()
    return _view(row) if row else None
