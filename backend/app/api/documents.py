"""Document routes. Gate order (each stage only runs if the previous one passed):

  1. body-size middleware (413)      app/body_limit.py: 2 MiB for content saves, 128 KiB elsewhere,
                                     counted on received bytes, never on Content-Length alone
  2. authentication (401)            CurrentUser
  3. CSRF on unsafe methods (403)    require_csrf
  4. UNLOCKED membership/role check  404 room_not_found / 403 insufficient_role (fast reject)
  5. request validation (422)        parse_json_body, then the document validator for content
  6. the transaction                 app/services/documents.py: room lock, RECHECK, write

The unlocked check keeps outsiders from taking the room lock; it is never the authority (the service
rechecks membership and role under the lock). The actor is always the session user.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Query, Request, Response

from app.dependencies.rooms import (
    MemberAccess,
    MemberAccessUnsafe,
    OwnerAccessUnsafe,
    RoomAccess,
    parse_uuid,
)
from app.documents.schema import SCHEMA_VERSION, InvalidDocument, validate_document
from app.errors import document_not_found, insufficient_role, invalid_document_content
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page
from app.request_body import json_body_docs, parse_json_body
from app.schemas.auth import ErrorBody
from app.schemas.documents import (
    ActiveDocumentOut,
    CreateDocumentRequest,
    DocumentOut,
    DocumentSummaryOut,
    RenameDocumentRequest,
    SaveContentOut,
    SaveContentRequest,
    SetActiveDocumentRequest,
)
from app.services import documents

router = APIRouter(tags=["documents"])

_COMMON = {
    401: {"model": ErrorBody, "description": "unauthenticated"},
    403: {"model": ErrorBody, "description": "csrf_failed / insufficient_role"},
    404: {"model": ErrorBody, "description": "room_not_found (also for non-members)"},
}
_ETAG_TOKEN = re.compile(r'\s*(?:W/)?("[^"]*"|\*)\s*')


def etag_for(content_version: int, metadata_version: int) -> str:
    return f'"c{content_version}m{metadata_version}"'


def _matches(header: str | None, etag: str) -> bool:
    """If-None-Match: a comma-separated list of entity tags (weak or strong) or *."""
    if not header:
        return False
    for part in header.split(","):
        found = _ETAG_TOKEN.fullmatch(part)
        if found and found.group(1) in ("*", etag):
            return True
    return False


def _document_out(view: documents.DocumentFull, role: str) -> DocumentOut:
    return DocumentOut(**view.__dict__, can_edit=role in documents.EDIT_ROLES)


def _summary_out(view: documents.DocumentSummary) -> DocumentSummaryOut:
    return DocumentSummaryOut(**{k: getattr(view, k) for k in DocumentSummaryOut.model_fields})


def _document_id(raw: str):
    parsed = parse_uuid(raw)
    if parsed is None:
        raise document_not_found()
    return parsed


@router.post(
    "/rooms/{room_id}/documents",
    response_model=DocumentOut,
    status_code=201,
    responses={
        200: {"model": DocumentOut, "description": "A matching retry: the existing document"},
        409: {
            "model": ErrorBody,
            "description": "document_request_id_reused / document_limit_reached",
        },
        **_COMMON,
    },
    openapi_extra=json_body_docs(CreateDocumentRequest),
)
async def create_document(
    request: Request, response: Response, access: OwnerAccessUnsafe
) -> DocumentOut:
    """Owner only. Creates the document AND makes it the room's active document (two room events).
    A retry with the same client_request_id answers 200 with the existing document and changes
    nothing (it never re-activates)."""
    body = await parse_json_body(request, CreateDocumentRequest)
    result = await documents.create_document(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        body.title,
        body.template_key,
        body.client_request_id,
    )
    response.status_code = 201 if result.created else 200
    return _document_out(result.document, access.role)


@router.get(
    "/rooms/{room_id}/documents",
    response_model=Page[DocumentSummaryOut],
    responses={**_COMMON, 422: {"model": ErrorBody, "description": "invalid_cursor"}},
)
async def list_documents(
    request: Request,
    access: MemberAccess,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
) -> Page[DocumentSummaryOut]:
    """Any active member. Metadata only (never content), newest first."""
    items, next_cursor = await documents.list_documents(
        request.app.state.sessionmaker, access.room_id, limit, cursor
    )
    return Page(items=[_summary_out(i) for i in items], next_cursor=next_cursor)


@router.get(
    "/rooms/{room_id}/documents/{document_id}",
    response_model=DocumentOut,
    responses={304: {"description": "If-None-Match matched the ETag"}, **_COMMON},
)
async def get_document(
    request: Request, access: MemberAccess, document_id: str
) -> DocumentOut | Response:
    """Any active member, any role; inactive documents stay readable. ETag is derived from
    (content_version, metadata_version); a matching If-None-Match gets 304 with no body."""
    view = await documents.get_document(
        request.app.state.sessionmaker, access.room_id, _document_id(document_id)
    )
    if view is None:
        raise document_not_found()
    etag = etag_for(view.content_version, view.metadata_version)
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag})
    out = _document_out(view, access.role)
    return Response(
        content=out.model_dump_json(), media_type="application/json", headers={"ETag": etag}
    )


@router.patch(
    "/rooms/{room_id}/documents/{document_id}",
    response_model=DocumentSummaryOut,
    responses=_COMMON,
    openapi_extra=json_body_docs(RenameDocumentRequest),
)
async def rename_document(
    request: Request, access: OwnerAccessUnsafe, document_id: str
) -> DocumentSummaryOut:
    """Owner only; accepts only `title`. Never touches content or content_version."""
    body = await parse_json_body(request, RenameDocumentRequest)
    view = await documents.rename_document(
        request.app.state.sessionmaker,
        access.user_id,
        access.room_id,
        _document_id(document_id),
        body.title,
    )
    return _summary_out(view)


@router.put(
    "/rooms/{room_id}/active-document",
    response_model=ActiveDocumentOut,
    responses={409: {"model": ErrorBody, "description": "selection_conflict"}, **_COMMON},
    openapi_extra=json_body_docs(SetActiveDocumentRequest),
)
async def set_active_document(request: Request, access: OwnerAccessUnsafe) -> ActiveDocumentOut:
    """Owner only. Choosing the already-active document is a no-op (no version bump, no event)."""
    body = await parse_json_body(request, SetActiveDocumentRequest)
    selection = await documents.set_active_document(
        request.app.state.sessionmaker,
        access.user_id,
        access.room_id,
        body.document_id,
        body.expected_selection_version,
    )
    return ActiveDocumentOut(**selection.__dict__)


@router.put(
    "/rooms/{room_id}/documents/{document_id}/content",
    response_model=SaveContentOut,
    responses={
        409: {"model": ErrorBody, "description": "document_version_conflict"},
        413: {"model": ErrorBody, "description": "payload_too_large (over 2 MiB)"},
        422: {"model": ErrorBody, "description": "invalid_document_content / asset_not_found"},
        429: {"model": ErrorBody, "description": "rate_limited (Retry-After set)"},
        **_COMMON,
    },
    openapi_extra=json_body_docs(SaveContentRequest),
)
async def save_content(
    request: Request, access: MemberAccessUnsafe, document_id: str
) -> SaveContentOut:
    """Owner or editor. Send the content_version you last saw; a stale one is 409 (even if the text
    is identical). The content is validated and canonicalized BEFORE any lock is taken; if the
    server's canonical form differs from what you sent, canonical_changed is true and the response
    carries the canonical content."""
    _require_editor(access)
    body = await parse_json_body(request, SaveContentRequest)
    if body.schema_version != SCHEMA_VERSION:
        raise invalid_document_content(
            [{"path": "/schema_version", "code": "invalid_value", "message": "must be 1"}]
        )
    settings = request.app.state.settings
    try:
        validated = validate_document(body.content, documents.limits_from_settings(settings))
    except InvalidDocument as exc:
        raise invalid_document_content(
            [{"path": e.path, "code": e.code, "message": e.message} for e in exc.errors]
        ) from None
    result = await documents.save_content(
        request.app.state.sessionmaker,
        settings,
        access.user_id,
        access.room_id,
        _document_id(document_id),
        body.expected_content_version,
        validated,
        documents.differs_from_canonical(body.content, validated.content),
    )
    return SaveContentOut(
        content_version=result.content_version,
        content_hash=result.content_hash,
        canonical_changed=result.canonical_changed,
        unchanged=result.unchanged,
        content=result.content,
    )


def _require_editor(access: RoomAccess) -> None:
    if access.role not in documents.EDIT_ROLES:
        raise insufficient_role()
