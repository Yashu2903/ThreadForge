"""Source routes (AI phase 1, checkpoint 2): list, device upload (intent / bytes / finalize), inspect,
download, replace and remove.

Gate order, as everywhere: authentication (401) -> CSRF on unsafe methods (403) -> room access
(404 for non-members) -> role (403 insufficient_role) -> body validation (422) -> the service, which
re-checks the role UNDER the room lock. Uploading never analyzes a file and never calls a provider.

Replace = a device upload whose intent names an existing `source_id`; it adds a version.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.dependencies.rooms import MemberAccess, MemberAccessUnsafe, RoomAccess, parse_uuid
from app.errors import (
    file_type_error,
    insufficient_role,
    invalid_source,
    source_not_found,
    source_upload_not_found,
)
from app.logging_config import APP_LOGGER
from app.request_body import json_body_docs, parse_json_body
from app.schemas.auth import ErrorBody
from app.services import sources as svc
from app.sources import files

log = logging.getLogger(f"{APP_LOGGER}.sources")
router = APIRouter(tags=["sources"])


# ---- schemas -------------------------------------------------------------------------------------------


class UploadIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # strict=False: JSON carries ids as strings; every other field stays strictly typed
    client_request_id: uuid.UUID = Field(strict=False)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str | None = Field(default=None, max_length=100)
    byte_size: int = Field(ge=1)
    source_id: uuid.UUID | None = Field(default=None, strict=False)


class VersionOut(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    version_no: int
    original_name: str
    kind: str
    media_type: str
    byte_size: int
    sha256: str
    analysis_state: str
    page_count: int | None
    text_page_count: int | None
    likely_scanned_pages: int | None
    image_count: int
    text_chars: int
    error_code: str | None
    created_by: uuid.UUID
    created_at: datetime


class SourceOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    display_name: str
    created_by: uuid.UUID
    created_at: datetime
    current_version: VersionOut | None
    version_count: int
    versions: list[VersionOut] | None = None  # only on the detail route


class SourceLimits(BaseModel):
    max_upload_bytes: int
    max_sources: int
    room_quota_bytes: int
    max_pages: int
    accepted: list[str]


class SourceListOut(BaseModel):
    sources: list[SourceOut]
    limits: SourceLimits
    used_sources: int
    used_bytes: int
    can_upload: bool
    can_download: bool


class UploadOut(BaseModel):
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


def _version_out(v: svc.VersionView) -> VersionOut:
    return VersionOut(**{k: getattr(v, k) for k in VersionOut.model_fields})


def _source_out(s: svc.SourceView, *, detail: bool = False) -> SourceOut:
    return SourceOut(
        id=s.id,
        room_id=s.room_id,
        display_name=s.display_name,
        created_by=s.created_by,
        created_at=s.created_at,
        current_version=_version_out(s.current_version) if s.current_version else None,
        version_count=s.version_count,
        versions=[_version_out(v) for v in s.versions] if detail else None,
    )


def _upload_out(u: svc.UploadView) -> UploadOut:
    return UploadOut(**{k: getattr(u, k) for k in UploadOut.model_fields})


def _require_editor(access: RoomAccess) -> None:
    if access.role not in svc.EDIT_ROLES:
        raise insufficient_role()


def _uuid_or(error, raw: str) -> uuid.UUID:
    parsed = parse_uuid(raw)
    if parsed is None:
        raise error()
    return parsed


# ---- list / inspect -----------------------------------------------------------------------------------


@router.get(
    "/rooms/{room_id}/sources",
    response_model=SourceListOut,
    responses={
        401: {"model": ErrorBody},
        404: {"model": ErrorBody, "description": "room_not_found"},
    },
)
async def list_sources(request: Request, access: MemberAccess) -> SourceListOut:
    """Any active member (viewers too): the active sources, their current version and analysis
    state, the room's usage and limits, and what this caller may do."""
    settings = request.app.state.settings
    sessionmaker = request.app.state.sessionmaker
    sources = await svc.list_sources(sessionmaker, access.room_id)
    used_bytes = await svc.room_bytes_used(sessionmaker, access.room_id)
    return SourceListOut(
        sources=[_source_out(s) for s in sources],
        limits=SourceLimits(
            max_upload_bytes=settings.source_max_upload_bytes,
            max_sources=settings.source_max_per_room,
            room_quota_bytes=settings.source_room_quota_bytes,
            max_pages=settings.source_max_pages,
            accepted=["pdf", "docx", "txt", "md"],
        ),
        used_sources=len(sources),
        used_bytes=used_bytes,
        can_upload=access.role in svc.EDIT_ROLES,
        can_download=access.role in settings.source_download_roles,
    )


@router.get(
    "/rooms/{room_id}/sources/{source_id}",
    response_model=SourceOut,
    responses={404: {"model": ErrorBody, "description": "room_not_found / source_not_found"}},
)
async def get_source(request: Request, access: MemberAccess, source_id: str) -> SourceOut:
    """Any active member: one source with every version and its inspection counts. (The extracted
    passages of an analyzed version arrive with checkpoint 3 and are for owners and editors only.)"""
    parsed = _uuid_or(source_not_found, source_id)
    view = await svc.get_source(request.app.state.sessionmaker, access.room_id, parsed)
    if view is None:
        raise source_not_found()
    return _source_out(view, detail=True)


# ---- upload: intent -----------------------------------------------------------------------------------


@router.post(
    "/rooms/{room_id}/source-uploads",
    response_model=UploadOut,
    status_code=201,
    responses={
        200: {"model": UploadOut, "description": "the same request id was sent again"},
        401: {"model": ErrorBody},
        403: {"model": ErrorBody, "description": "csrf_failed / insufficient_role"},
        404: {"model": ErrorBody, "description": "room_not_found / source_not_found"},
        409: {
            "model": ErrorBody,
            "description": "quota_exceeded / too_many_open_uploads / source_upload_id_reused",
        },
        413: {"model": ErrorBody, "description": "payload_too_large (declared size)"},
        415: {"model": ErrorBody, "description": "unsupported_media_type"},
        422: {"model": ErrorBody},
        429: {"model": ErrorBody, "description": "rate_limited"},
    },
    openapi_extra=json_body_docs(UploadIntentRequest),
)
async def create_upload(
    request: Request, response: Response, access: MemberAccessUnsafe
) -> UploadOut:
    """Step 1 of 3. Owner or editor. Declares the file (sanitized name, size, optional type) and
    reserves room for it. The same `client_request_id` with the same fields returns the same upload
    (200); with different fields it is 409. Include `source_id` to REPLACE an existing source."""
    _require_editor(access)
    body = await parse_json_body(request, UploadIntentRequest)
    view, created = await svc.create_upload_intent(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        client_request_id=body.client_request_id,
        filename=body.filename,
        declared_type=body.media_type,
        declared_size=body.byte_size,
        source_id=body.source_id,
    )
    if not created:
        response.status_code = 200
    return _upload_out(view)


@router.get(
    "/rooms/{room_id}/source-uploads/{upload_id}",
    response_model=UploadOut,
    responses={
        404: {"model": ErrorBody, "description": "room_not_found / source_upload_not_found"}
    },
)
async def get_upload(request: Request, access: MemberAccess, upload_id: str) -> UploadOut:
    """The creator's view of one upload (anyone else gets 404)."""
    parsed = _uuid_or(source_upload_not_found, upload_id)
    view = await svc.get_upload(
        request.app.state.sessionmaker, access.room_id, access.user_id, parsed
    )
    return _upload_out(view)


# ---- upload: bytes ------------------------------------------------------------------------------------


@router.put(
    "/rooms/{room_id}/source-uploads/{upload_id}/content",
    response_model=UploadOut,
    responses={
        401: {"model": ErrorBody},
        403: {"model": ErrorBody, "description": "csrf_failed / insufficient_role"},
        404: {"model": ErrorBody, "description": "room_not_found / source_upload_not_found"},
        409: {"model": ErrorBody, "description": "source_upload_not_open"},
        413: {"model": ErrorBody, "description": "payload_too_large"},
        415: {"model": ErrorBody, "description": "unsupported_media_type"},
        422: {"model": ErrorBody, "description": "invalid_source (size_mismatch)"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def put_content(request: Request, access: MemberAccessUnsafe, upload_id: str) -> UploadOut:
    """Step 2 of 3. The raw file bytes as the body, streamed and counted as they arrive (the
    Content-Length header is never trusted). The count must equal the size declared in step 1.
    The first bytes must be the real signature of the file's type. A repeated PUT after success
    returns the stored upload without reading the body."""
    _require_editor(access)
    parsed = _uuid_or(source_upload_not_found, upload_id)
    sessionmaker = request.app.state.sessionmaker
    storage = request.app.state.storage
    view, accepts = await svc.precheck_content(sessionmaker, access.room_id, access.user_id, parsed)
    if not accepts:
        return _upload_out(view)

    fd, tmp = storage.temp_file()
    cleanup = [tmp]
    try:
        digest = hashlib.sha256()
        total = 0
        head = b""
        too_big = False
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                total += len(chunk)
                if total > view.declared_size:
                    too_big = True
                    break
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                digest.update(chunk)
                out.write(chunk)
        if too_big or total != view.declared_size:
            await svc.end_upload(
                sessionmaker,
                storage,
                access.room_id,
                parsed,
                state="failed",
                error_code="size_mismatch",
            )
            raise invalid_source(
                "size_mismatch",
                "The bytes received do not match the size declared for this upload.",
            )
        try:
            files.check_signature(view.kind, head)
        except files.UnsupportedFile as exc:
            await svc.end_upload(
                sessionmaker, storage, access.room_id, parsed, state="failed", error_code=exc.code
            )
            raise file_type_error(exc.code, exc.message) from None

        key = storage.new_key()
        storage.put(svc.NAMESPACE, key, tmp)  # atomic move; the row is updated next
        cleanup.remove(tmp)
        try:
            updated = await svc.record_bytes(
                sessionmaker,
                access.user_id,
                access.room_id,
                parsed,
                storage_key=key,
                byte_size=total,
                sha256=digest.digest(),
            )
        except BaseException:
            storage.delete(svc.NAMESPACE, key)  # refused (state changed, role lost): keep nothing
            raise
        return _upload_out(updated)
    finally:
        for path in cleanup:
            with contextlib.suppress(OSError):
                path.unlink()


# ---- upload: finalize / cancel ------------------------------------------------------------------------


@router.post(
    "/rooms/{room_id}/source-uploads/{upload_id}/finalize",
    response_model=SourceOut,
    status_code=201,
    responses={
        401: {"model": ErrorBody},
        403: {"model": ErrorBody, "description": "csrf_failed / insufficient_role"},
        404: {
            "model": ErrorBody,
            "description": "room_not_found / source_upload_not_found / source_not_found",
        },
        409: {"model": ErrorBody, "description": "source_upload_not_open / quota_exceeded"},
        415: {"model": ErrorBody, "description": "unsupported_media_type"},
        422: {"model": ErrorBody, "description": "invalid_source (reason names the problem)"},
    },
)
async def finalize_upload(
    request: Request, access: MemberAccessUnsafe, upload_id: str
) -> SourceOut:
    """Step 3 of 3. Inspects the stored file in the sandbox (validity, pages, text layer, scans,
    images; nothing is extracted, nothing leaves the server), then creates the source (or the new
    version of the source being replaced). A file that fails inspection is deleted and the upload is
    `failed` with the reason. Repeating the call returns the same source."""
    _require_editor(access)
    parsed = _uuid_or(source_upload_not_found, upload_id)
    view = await svc.finalize_upload(
        request.app.state.sessionmaker,
        request.app.state.settings,
        request.app.state.storage,
        request.app.state.source_slots,
        access.user_id,
        access.room_id,
        parsed,
    )
    return _source_out(view, detail=True)


@router.delete(
    "/rooms/{room_id}/source-uploads/{upload_id}",
    response_model=UploadOut,
    responses={
        404: {"model": ErrorBody, "description": "room_not_found / source_upload_not_found"}
    },
)
async def cancel_upload(request: Request, access: MemberAccessUnsafe, upload_id: str) -> UploadOut:
    """The creator abandons an unfinished upload; its bytes are deleted now. A finished upload
    is returned unchanged (remove the source instead)."""
    _require_editor(access)
    parsed = _uuid_or(source_upload_not_found, upload_id)
    view = await svc.cancel_upload(
        request.app.state.sessionmaker,
        request.app.state.storage,
        access.user_id,
        access.room_id,
        parsed,
    )
    return _upload_out(view)


# ---- download / remove --------------------------------------------------------------------------------


@router.get(
    "/rooms/{room_id}/sources/{source_id}/versions/{version_id}/download",
    responses={
        200: {"description": "the original file, as an attachment"},
        403: {"model": ErrorBody, "description": "insufficient_role (SOURCE_DOWNLOAD_ROLES)"},
        404: {"model": ErrorBody, "description": "room_not_found / source_not_found"},
    },
)
async def download_version(
    request: Request, access: MemberAccess, source_id: str, version_id: str
) -> Response:
    """The original bytes, for the roles in SOURCE_DOWNLOAD_ROLES (checked on every request).
    Always `attachment`, `nosniff`, a sandboxing CSP and `no-store`; the name is the sanitized one."""
    if access.role not in request.app.state.settings.source_download_roles:
        raise insufficient_role()
    sid = _uuid_or(source_not_found, source_id)
    vid = _uuid_or(source_not_found, version_id)
    stored = await svc.get_version_file(request.app.state.sessionmaker, access.room_id, sid, vid)
    if stored is None:
        raise source_not_found()
    path = svc.file_path(request.app.state.storage, stored.storage_key)
    if not path.is_file():
        log.warning("action=source_file_missing room_id=%s version_id=%s", access.room_id, vid)
        raise source_not_found()
    media_type = stored.version.media_type
    if media_type.startswith("text/"):
        media_type += "; charset=utf-8"
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Content-Disposition": files.content_disposition(stored.version.original_name),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete(
    "/rooms/{room_id}/sources/{source_id}",
    status_code=204,
    responses={
        401: {"model": ErrorBody},
        403: {"model": ErrorBody, "description": "csrf_failed / insufficient_role"},
        404: {"model": ErrorBody, "description": "room_not_found / source_not_found"},
    },
)
async def remove_source(request: Request, access: MemberAccessUnsafe, source_id: str) -> Response:
    """Owner: any source. Editor: only one they created. Viewer: never. The original file is
    deleted immediately; already-published chat answers keep their text (docs/ai.md)."""
    parsed = _uuid_or(source_not_found, source_id)
    if access.role not in svc.EDIT_ROLES:
        raise insufficient_role()
    await svc.remove_source(
        request.app.state.sessionmaker,
        request.app.state.storage,
        access.user_id,
        access.room_id,
        parsed,
    )
    return Response(status_code=204)
