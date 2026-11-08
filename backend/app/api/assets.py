"""Asset routes: upload an image (owner/editor) and download one (any active member).

Gate order for the upload: body size (streaming, counted on received bytes) -> authentication ->
CSRF -> unlocked membership/role pre-check -> Content-Type check -> stream to a temp file -> process
(real signature, pixel/animation limits, re-encode) -> store under an opaque key -> locked
transaction (role recheck, rate limit, quotas, insert). Whatever fails after the body started, the
temporary files are removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.assets_pipeline import MEDIA_TYPES, ImageRejected, process_image
from app.dependencies.rooms import MemberAccess, MemberAccessUnsafe, RoomAccess, parse_uuid
from app.errors import (
    asset_not_found,
    insufficient_role,
    invalid_image,
    payload_too_large,
    unsupported_media_type,
)
from app.schemas.auth import ErrorBody
from app.services import assets

router = APIRouter(tags=["assets"])
ALLOWED_TYPES = set(MEDIA_TYPES.values())


class AssetOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    media_type: str
    byte_size: int
    width_px: int
    height_px: int
    sha256: str
    state: str
    created_at: datetime


def _out(view: assets.AssetView) -> AssetOut:
    return AssetOut(**{k: getattr(view, k) for k in AssetOut.model_fields})


def _require_editor(access: RoomAccess) -> None:
    if access.role not in assets.EDIT_ROLES:
        raise insufficient_role()


@router.post(
    "/rooms/{room_id}/assets",
    response_model=AssetOut,
    status_code=201,
    responses={
        401: {"model": ErrorBody},
        403: {"model": ErrorBody, "description": "csrf_failed / insufficient_role"},
        404: {"model": ErrorBody, "description": "room_not_found"},
        409: {"model": ErrorBody, "description": "quota_exceeded"},
        413: {"model": ErrorBody, "description": "payload_too_large"},
        415: {"model": ErrorBody, "description": "unsupported_media_type"},
        422: {"model": ErrorBody, "description": "invalid_image"},
        429: {"model": ErrorBody, "description": "rate_limited"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                t: {"schema": {"type": "string", "format": "binary"}} for t in sorted(ALLOWED_TYPES)
            },
        }
    },
)
async def upload_asset(request: Request, access: MemberAccessUnsafe) -> AssetOut:
    """Raw image bytes in the body; Content-Type must be image/png, image/jpeg or image/webp. The
    stored file is a clean re-encoding (no EXIF/GPS/embedded payload); SVG, GIF and animated images
    are refused. The asset starts PENDING and is attached by the first save that references it."""
    _require_editor(access)
    settings = request.app.state.settings
    storage = request.app.state.storage
    declared = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if declared not in ALLOWED_TYPES:
        raise unsupported_media_type()

    fd, tmp = storage.temp_file()
    cleanup = [tmp]
    try:
        total = 0
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                total += len(chunk)
                if total > settings.asset_max_upload_bytes:
                    raise payload_too_large()
                out.write(chunk)
        if total == 0:
            raise invalid_image("empty", "The upload was empty.")
        try:
            async with request.app.state.asset_slots:
                processed = await asyncio.to_thread(
                    process_image, tmp, declared, settings.asset_max_pixels
                )
        except ImageRejected as exc:
            if exc.code in ("unsupported_media_type", "type_mismatch"):
                raise unsupported_media_type(exc.message) from None
            raise invalid_image(exc.code, exc.message) from None
        cleanup.append(processed.path)

        key = storage.new_key()
        storage.put("assets", key, processed.path)  # atomic move; the row is inserted next
        cleanup.remove(processed.path)
        try:
            view = await assets.create_asset(
                request.app.state.sessionmaker,
                settings,
                access.user_id,
                access.room_id,
                processed,
                key,
            )
        except BaseException:
            storage.delete(
                "assets", key
            )  # refused (quota, rate limit, role): do not keep the bytes
            raise
        return _out(view)
    finally:
        for path in cleanup:
            with contextlib.suppress(OSError):
                path.unlink()


@router.get(
    "/rooms/{room_id}/assets/{asset_id}",
    responses={
        200: {"content": {t: {} for t in sorted(ALLOWED_TYPES)}, "description": "the image"},
        304: {"description": "If-None-Match matched"},
        404: {"model": ErrorBody, "description": "room_not_found / asset_not_found"},
    },
)
async def get_asset(request: Request, access: MemberAccess, asset_id: str) -> Response:
    """Any active member, checked on EVERY request. Content-Type is the validated type (never
    from the uploader), nosniff, `Cache-Control: private` with an ETag (the SHA-256), so a browser
    may keep it but must revalidate."""
    parsed = parse_uuid(asset_id)
    view = (
        await assets.get_asset(request.app.state.sessionmaker, access.room_id, parsed)
        if parsed
        else None
    )
    if view is None or view.state == "expired":
        raise asset_not_found()
    etag = f'"{view.sha256}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": "inline",
    }
    match = request.headers.get("if-none-match", "")
    if any(
        part.strip().removeprefix("W/") in (etag, "*") for part in match.split(",") if part.strip()
    ):
        return Response(status_code=304, headers=headers)
    path = request.app.state.storage.path_for("assets", view.storage_key)
    if not path.is_file():
        raise asset_not_found()  # the row exists but the bytes are gone (a storage problem)
    return FileResponse(path, media_type=view.media_type, headers=headers)
