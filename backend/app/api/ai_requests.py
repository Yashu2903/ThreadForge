"""`@AI` request routes (AI phase 1, checkpoint 4).

Gate order as everywhere: authentication (401) -> CSRF on unsafe methods (403) -> room access (404 for
non-members) -> role (403 insufficient_role) -> body validation (422) -> the service, which re-checks
the role and the whole AI gate (can_write, switches, consent, keys, capacity, caps) under the room lock.

WHO MAY DO WHAT (the brief's table):
  create / retry / cancel own      owner or editor with can_write on the anchor document
  cancel any request in the room   the owner
  progress, context summary        the requester, and owners/editors with can_write on the request's
                                   anchor document; viewers: 403 insufficient_role (they see only the
                                   ordinary chat message)
  the published result             any member (viewers too)
  citation text and images         roles in SOURCE_DOWNLOAD_ROLES (default all three)

These routes never call a provider.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.dependencies.rooms import MemberAccess, MemberAccessUnsafe, RoomAccess, parse_uuid
from app.errors import AppError, ai_request_not_found, ai_write_access_required, insufficient_role
from app.request_body import json_body_docs, parse_json_body
from app.schemas.ai import JsonUuid
from app.schemas.auth import ErrorBody
from app.services import ai_requests as svc
from app.services.permissions import decide_write

router = APIRouter(tags=["ai-requests"])
_HEX64 = "0123456789abcdef"


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # strict=False: JSON carries ids as strings; every other field stays strictly typed
    client_request_id: uuid.UUID = Field(strict=False)
    instruction: str = Field(min_length=1, max_length=4000)
    task_hint: Literal["auto", "ask", "summarize", "compare"] = "auto"
    document_id: uuid.UUID = Field(strict=False)
    include_document_content: bool = False
    document_scope: Literal["none", "whole", "blocks"] = "none"
    selected_block_ids: list[JsonUuid] = Field(default_factory=list, max_length=50)
    selection_hash: str | None = Field(default=None, min_length=64, max_length=64)
    source_version_ids: list[JsonUuid] = Field(default_factory=list, max_length=10)
    include_chat_context: bool = False
    chat_message_ids: list[JsonUuid] = Field(default_factory=list, max_length=20)
    confirm_large: bool = False

    @field_validator("instruction")
    @classmethod
    def _text(cls, value: str) -> str:
        cleaned = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32).strip()
        if not cleaned:
            raise ValueError("The instruction is empty.")
        return cleaned

    @field_validator("selection_hash")
    @classmethod
    def _hash(cls, value: str | None) -> str | None:
        if value is not None and (value != value.lower() or any(c not in _HEX64 for c in value)):
            raise ValueError("selection_hash must be 64 lowercase hex characters.")
        return value


class SourceRefOut(BaseModel):
    version_id: uuid.UUID
    source_id: uuid.UUID
    name: str
    version_no: int
    analysis_state: str
    error_code: str | None
    kind: str


class RequestOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    message_id: uuid.UUID
    requested_by: uuid.UUID
    state: str
    stage: str | None
    stage_label: str | None
    task_hint: str
    task_type: str | None
    error_code: str | None
    error_detail: dict[str, Any] | None
    attempt: int
    max_attempts: int
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    document_id: uuid.UUID
    include_document_content: bool
    document_scope: str
    include_chat_context: bool
    chat_message_count: int
    sources: list[SourceRefOut]
    queue_position: int | None
    estimate: dict[str, Any]
    key_sources: dict[str, str]
    cancel_requested: bool
    reply_message_id: uuid.UUID | None
    can_cancel: bool
    can_retry: bool


class ResultOut(BaseModel):
    request_id: uuid.UUID
    reply_message_id: uuid.UUID | None
    task_type: str
    redacted: bool
    answer: dict[str, Any] | None
    citations: list[dict[str, Any]]
    coverage: dict[str, Any]
    models: dict[str, Any]
    created_at: datetime
    citation_text_visible: bool


def _out(settings, view: svc.RequestView, access: RoomAccess) -> RequestOut:
    terminal = view.state in svc.TERMINAL
    mine = view.requested_by == access.user_id
    return RequestOut(
        id=view.id,
        room_id=view.room_id,
        message_id=view.message_id,
        requested_by=view.requested_by,
        state=view.state,
        stage=view.stage,
        stage_label=view.stage_label,
        task_hint=view.task_hint,
        task_type=view.task_type,
        error_code=view.error_code,
        error_detail=view.error_detail,
        attempt=view.attempt,
        max_attempts=settings.ai_request_max_attempts,
        created_at=view.created_at,
        queued_at=view.queued_at,
        started_at=view.started_at,
        finished_at=view.finished_at,
        document_id=view.document_id,
        include_document_content=view.include_document_content,
        document_scope=view.document_scope,
        include_chat_context=view.include_chat_context,
        chat_message_count=view.chat_message_count,
        sources=[SourceRefOut(**vars(s)) for s in view.sources],
        queue_position=view.queue_position,
        estimate=view.estimate,
        key_sources={
            "generation": view.key_source_generation,
            "embedding": view.key_source_embedding,
        },
        cancel_requested=view.cancel_requested,
        reply_message_id=view.reply_message_id,
        can_cancel=(not terminal)
        and (access.role == "owner" or mine)
        and view.state != "cancelling",
        can_retry=mine
        and view.state in ("failed", "expired", "cancelled")
        and view.attempt < settings.ai_request_max_attempts,
    )


def _request_id(raw: str) -> uuid.UUID:
    parsed = parse_uuid(raw)
    if parsed is None:
        raise ai_request_not_found()
    return parsed


async def _load_visible(request: Request, access: RoomAccess, request_id: uuid.UUID):
    """Progress, context and manifest: the requester, or an owner/editor who can write the anchor
    document. A viewer is refused outright; a request of another room is simply not found."""
    sm = request.app.state.sessionmaker
    view = await svc.get_request(sm, access.room_id, request_id)
    if view is None:
        raise ai_request_not_found()
    if access.role not in svc.EDIT_ROLES:
        raise insufficient_role()
    if view.requested_by != access.user_id:
        async with sm() as db:
            decision = await decide_write(
                db, request.app.state.settings, access.user_id, view.document_id
            )
        if not decision.allowed:
            raise ai_write_access_required()
    return view


@router.post(
    "/rooms/{room_id}/ai-requests",
    response_model=RequestOut,
    status_code=201,
    responses={
        200: {"model": RequestOut, "description": "the same client_request_id was sent again"},
        403: {
            "model": ErrorBody,
            "description": "csrf_failed / insufficient_role / consent_required / ai_write_access_required",
        },
        404: {"model": ErrorBody, "description": "room_not_found / document_not_found"},
        409: {
            "model": ErrorBody,
            "description": "confirmation_required / source_unavailable / key_unavailable / analysis_cap_exceeded / ai_request_id_reused / ai_disabled_for_room / ai_anchor_required",
        },
        422: {"model": ErrorBody, "description": "invalid_ai_request / ai_context_required"},
        429: {"model": ErrorBody, "description": "ai_capacity / rate_limited / budget_exhausted"},
        503: {
            "model": ErrorBody,
            "description": "ai_not_configured / ai_disabled / ai_unavailable",
        },
    },
    openapi_extra=json_body_docs(CreateRequest),
)
async def create_request(
    request: Request, response: Response, access: MemberAccessUnsafe
) -> RequestOut:
    """Send an `@AI` request. Owner or editor with write access to `document_id` (the anchor). The
    actor, the room, the role and every key choice come from the server, never from the body."""
    if access.role not in svc.EDIT_ROLES:
        raise insufficient_role()
    body = await parse_json_body(request, CreateRequest)
    settings = request.app.state.settings
    view, created = await svc.create_request(
        request.app.state.sessionmaker,
        settings,
        access.user_id,
        access.room_id,
        svc.RequestInput(
            client_request_id=body.client_request_id,
            instruction=body.instruction,
            task_hint=body.task_hint,
            document_id=body.document_id,
            include_document_content=body.include_document_content,
            document_scope=body.document_scope,
            selected_block_ids=tuple(body.selected_block_ids),
            selection_hash=body.selection_hash,
            source_version_ids=tuple(body.source_version_ids),
            include_chat_context=body.include_chat_context,
            chat_message_ids=tuple(body.chat_message_ids),
            confirm_large=body.confirm_large,
        ),
    )
    if not created:
        response.status_code = 200
    return _out(settings, view, access)


@router.get(
    "/rooms/{room_id}/ai-requests/{request_id}",
    response_model=RequestOut,
    responses={
        403: {"model": ErrorBody, "description": "insufficient_role / ai_write_access_required"},
        404: {"model": ErrorBody, "description": "room_not_found / ai_request_not_found"},
    },
)
async def get_request(request: Request, access: MemberAccess, request_id: str) -> RequestOut:
    """Progress of one request (stage labels come from here: poll with backoff while a card shows)."""
    view = await _load_visible(request, access, _request_id(request_id))
    return _out(request.app.state.settings, view, access)


@router.get(
    "/rooms/{room_id}/ai-requests/{request_id}/context",
    responses={404: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)
async def get_context(request: Request, access: MemberAccess, request_id: str) -> dict[str, Any]:
    """The authorized context summary: WHAT went into the request (files and versions, counts of
    passages and images, the document capture's update counter, number of chat messages, models,
    exclusions, the coverage statement). Never the texts themselves."""
    rid = _request_id(request_id)
    await _load_visible(request, access, rid)
    summary = await svc.get_manifest_summary(request.app.state.sessionmaker, access.room_id, rid)
    return {"request_id": str(rid), "context": summary}


@router.post(
    "/rooms/{room_id}/ai-requests/{request_id}/cancel",
    response_model=RequestOut,
    responses={409: {"model": ErrorBody, "description": "ai_request_not_cancellable"}},
)
async def cancel_request(
    request: Request, access: MemberAccessUnsafe, request_id: str
) -> RequestOut:
    """The requester, or the owner (any request in the room)."""
    rid = _request_id(request_id)
    await svc.cancel_request(request.app.state.sessionmaker, access.user_id, access.room_id, rid)
    view = await svc.get_request(request.app.state.sessionmaker, access.room_id, rid)
    if view is None:
        raise ai_request_not_found()
    return _out(request.app.state.settings, view, access)


@router.post(
    "/rooms/{room_id}/ai-requests/{request_id}/retry",
    response_model=RequestOut,
    responses={
        409: {"model": ErrorBody, "description": "ai_request_not_retryable / source_unavailable"}
    },
)
async def retry_request(
    request: Request, access: MemberAccessUnsafe, request_id: str
) -> RequestOut:
    """The requester retries a failed, expired or cancelled request (up to AI_REQUEST_MAX_ATTEMPTS
    attempts). Same inputs and pinned versions; everything is re-checked."""
    view = await svc.retry_request(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        _request_id(request_id),
    )
    return _out(request.app.state.settings, view, access)


def _may_read_citations(settings, access: RoomAccess) -> bool:
    return access.role in settings.source_download_roles


@router.get(
    "/rooms/{room_id}/ai-requests/{request_id}/result",
    response_model=ResultOut,
    responses={404: {"model": ErrorBody, "description": "room_not_found / ai_request_not_found"}},
)
async def get_result(request: Request, access: MemberAccess, request_id: str) -> ResultOut:
    """The published answer (any member, viewers included). Citation excerpts and quotes are included
    only for roles in SOURCE_DOWNLOAD_ROLES; everyone sees the numbers, labels and locators."""
    rid = _request_id(request_id)
    row = await svc.get_result(request.app.state.sessionmaker, access.room_id, rid)
    if row is None:
        raise ai_request_not_found()
    visible = _may_read_citations(request.app.state.settings, access)
    citations = row.citations or []
    if not visible:
        citations = [
            {k: v for k, v in c.items() if k not in ("excerpt", "quotes")} for c in citations
        ]
    return ResultOut(
        request_id=rid,
        reply_message_id=row.reply_message_id,
        task_type=row.task_type,
        redacted=row.redacted_at is not None,
        answer=row.answer,
        citations=citations,
        coverage=row.coverage or {},
        models=row.models or {},
        created_at=row.created_at,
        citation_text_visible=visible,
    )


@router.get(
    "/rooms/{room_id}/ai-requests/{request_id}/citations/{n}",
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def resolve_citation(
    request: Request, access: MemberAccess, request_id: str, n: int
) -> dict[str, Any]:
    """One citation resolved: the cited passage's excerpt and any quotes. Roles in SOURCE_DOWNLOAD_ROLES.
    A citation to a removed file is a stub ('source removed')."""
    rid = _request_id(request_id)
    if not _may_read_citations(request.app.state.settings, access):
        raise insufficient_role()
    row = await svc.get_result(request.app.state.sessionmaker, access.room_id, rid)
    if row is None:
        raise ai_request_not_found()
    for c in row.citations or []:
        if c.get("n") == n:
            return c
    raise AppError(404, "citation_not_found", "Citation not found.")


@router.get(
    "/rooms/{room_id}/ai-requests/{request_id}/citations/{n}/image",
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def citation_image(
    request: Request, access: MemberAccess, request_id: str, n: int
) -> FileResponse:
    """The figure a citation points to (the stored, re-encoded image), for roles in
    SOURCE_DOWNLOAD_ROLES. 404 once the source was removed."""
    rid = _request_id(request_id)
    settings = request.app.state.settings
    if not _may_read_citations(settings, access):
        raise insufficient_role()
    row = await svc.get_result(request.app.state.sessionmaker, access.room_id, rid)
    if row is None:
        raise ai_request_not_found()
    cite = next((c for c in (row.citations or []) if c.get("n") == n), None)
    chunk_id = (cite or {}).get("figure_chunk_id")
    if not cite or not chunk_id or cite.get("source_removed"):
        raise AppError(404, "citation_not_found", "Citation not found.")
    figure = await svc.figure_of(
        request.app.state.sessionmaker, access.room_id, uuid.UUID(chunk_id)
    )
    if figure is None:
        raise AppError(404, "citation_not_found", "Citation not found.")
    path = request.app.state.storage.path_for("figures", figure.storage_key)
    if not path.is_file():
        raise AppError(404, "citation_not_found", "Citation not found.")
    return FileResponse(
        path,
        media_type=figure.media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": "inline",
        },
    )
