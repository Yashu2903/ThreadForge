"""Edit proposals and drafts: routes (AI phase 2).

Gate order as everywhere: authentication (401) -> CSRF on unsafe methods (403) -> room access (404 for
non-members) -> the proposal exists in THIS room (404 ai_proposal_not_found, whatever the reason: a proposal of
another room and a missing one are indistinguishable) -> who may see / do it (403) -> the service, which
rechecks everything under the locks.

WHO MAY DO WHAT
  see a proposal, its preview, its context   the requester, and any owner or editor with CURRENT write access to
                                             the target document; for a draft, the requester and the owner(s).
                                             A viewer sees only the ordinary chat message.
  apply an edit                              the requester or anyone with current write access to the target.
                                             The collaboration service rechecks it, atomically, at the moment of
                                             the change.
  reject                                     the same people; for a draft, the requester or the owner.
  regenerate (after a conflict)              the same people as apply.
  publish a draft                            the OWNER only, through the existing create-document path.
Whole-proposal actions only. First apply wins; apply is idempotent. These routes never call a model.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.ai.edit import collab_client
from app.api.ai_requests import RequestOut
from app.api.ai_requests import _out as request_out
from app.dependencies.rooms import MemberAccess, MemberAccessUnsafe, RoomAccess, parse_uuid
from app.errors import (
    AppError,
    ai_proposal_not_found,
    ai_write_access_required,
    insufficient_role,
    proposal_not_pending,
)
from app.request_body import parse_json_body
from app.schemas.auth import ErrorBody
from app.services import ai_proposals as svc
from app.services import ai_requests as requests_svc
from app.services.permissions import EDIT_ROLES

router = APIRouter(tags=["ai-proposals"])


class ProposalOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    room_id: uuid.UUID
    kind: str  # edit | draft
    state: str  # pending | applying | applied | rejected | conflicted | expired | superseded
    document_id: uuid.UUID | None
    document_title: str | None
    summary: str
    operation_count: int
    preview: dict[str, Any]
    citations: list[dict[str, Any]]
    citation_text_visible: bool
    scope: dict[str, Any]
    captured_counter: int | None
    created_by: uuid.UUID
    created_at: datetime
    expires_at: datetime
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    resolution_code: str | None
    regenerated_from: uuid.UUID | None
    published_document_id: uuid.UUID | None
    can_apply: bool
    can_reject: bool
    can_regenerate: bool
    can_publish: bool


class ApplyOut(BaseModel):
    state: str
    already: bool
    synced: bool  # connected editors already have the change
    durable: bool  # it is saved in PostgreSQL with its receipt
    counter: int | None
    connected_users: int = 0


class RegenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    client_request_id: uuid.UUID = Field(strict=False)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    client_request_id: uuid.UUID = Field(strict=False)


class PublishOut(BaseModel):
    document_id: uuid.UUID
    title: str
    created: bool
    is_active: bool


def _proposal_id(raw: str) -> uuid.UUID:
    parsed = parse_uuid(raw)
    if parsed is None:
        raise ai_proposal_not_found()
    return parsed


def _out(
    settings: Any, access: RoomAccess, view: svc.ProposalRow, perms: svc.Permissions
) -> ProposalOut:
    r = view.row
    visible = access.role in settings.source_download_roles
    citations = r.citations or []
    if not visible:
        citations = [
            {k: v for k, v in c.items() if k not in ("excerpt", "quotes")} for c in citations
        ]
    preview = r.preview or {}
    count = (
        len(r.operations)
        if r.kind == "edit" and isinstance(r.operations, list)
        else len((r.operations or {}).get("blocks", []))
    )
    return ProposalOut(
        id=r.id,
        request_id=r.request_id,
        room_id=r.room_id,
        kind=r.kind,
        state=view.effective_state,
        document_id=r.document_id,
        document_title=r.document_title,
        summary=str(preview.get("summary") or ""),
        operation_count=count,
        preview=preview,
        citations=citations,
        citation_text_visible=visible,
        scope=r.scope or {},
        captured_counter=r.captured_counter,
        created_by=r.created_by,
        created_at=r.created_at,
        expires_at=r.expires_at,
        resolved_by=r.resolved_by,
        resolved_at=r.resolved_at,
        resolution_code=r.resolution_code,
        regenerated_from=r.regenerated_from,
        published_document_id=r.published_document_id,
        can_apply=perms.can_apply,
        can_reject=perms.can_reject,
        can_regenerate=perms.can_regenerate,
        can_publish=perms.can_publish,
    )


async def _visible(
    request: Request, access: RoomAccess, raw_id: str
) -> tuple[uuid.UUID, svc.ProposalRow, svc.Permissions]:
    pid = _proposal_id(raw_id)
    sm = request.app.state.sessionmaker
    settings = request.app.state.settings
    view = await svc.get_proposal(sm, access.room_id, pid)
    if view is None:
        raise ai_proposal_not_found()
    if access.role not in EDIT_ROLES:
        raise insufficient_role()
    async with sm() as db:
        if not await svc.may_view(
            db, settings, role=access.role, user_id=access.user_id, row=view.row
        ):
            raise ai_write_access_required()
        perms = await svc.permissions_for(
            db, settings, role=access.role, user_id=access.user_id, view=view
        )
    return pid, view, perms


@router.get(
    "/rooms/{room_id}/ai-proposals/{proposal_id}",
    response_model=ProposalOut,
    responses={
        403: {"model": ErrorBody, "description": "insufficient_role / ai_write_access_required"},
        404: {"model": ErrorBody, "description": "room_not_found / ai_proposal_not_found"},
    },
)
async def get_proposal(request: Request, access: MemberAccess, proposal_id: str) -> ProposalOut:
    """One proposal with its preview (what each change would replace, with the model's reason and its
    citation numbers; for a draft, the whole new document). Expired proposals stay readable."""
    _pid, view, perms = await _visible(request, access, proposal_id)
    return _out(request.app.state.settings, access, view, perms)


@router.post(
    "/rooms/{room_id}/ai-proposals/{proposal_id}/apply",
    response_model=ApplyOut,
    responses={
        403: {
            "model": ErrorBody,
            "description": "csrf_failed / insufficient_role / ai_write_access_required",
        },
        404: {"model": ErrorBody, "description": "room_not_found / ai_proposal_not_found"},
        409: {
            "model": ErrorBody,
            "description": "proposal_conflicted / document_busy (retryable) / proposal_rejected / proposal_expired / proposal_applying ...",
        },
        503: {"model": ErrorBody, "description": "collab_unavailable"},
    },
)
async def apply_proposal(
    request: Request, access: MemberAccessUnsafe, proposal_id: str
) -> ApplyOut:
    """Apply an EDIT proposal to the live shared document, atomically and at most once. The collaboration
    service does the change (docs/ai.md, "Applying an edit proposal"); this route only decides who may ask and
    translates the outcome. Idempotent: applying an applied proposal reports it again and changes nothing."""
    pid, view, _perms = await _visible(request, access, proposal_id)
    row = view.row
    if row.kind != "edit" or row.document_id is None:
        raise proposal_not_pending(row.state)
    status, body = await collab_client.apply_proposal(
        request.app.state.settings,
        proposal_id=pid,
        document_id=row.document_id,
        user_id=access.user_id,
        transport=getattr(request.app.state, "collab_transport", None),
    )
    if status == 200:
        return ApplyOut(
            state=str(body.get("state", "applied")),
            already=bool(body.get("already")),
            synced=bool(body.get("synced")),
            durable=bool(body.get("durable")),
            counter=body.get("counter"),
            connected_users=int(body.get("connected_users") or 0),
        )
    raise collab_client.error_for(status, body)


@router.post(
    "/rooms/{room_id}/ai-proposals/{proposal_id}/reject",
    response_model=ProposalOut,
    responses={
        409: {"model": ErrorBody, "description": "proposal_applying / proposal_applied ..."}
    },
)
async def reject_proposal(
    request: Request, access: MemberAccessUnsafe, proposal_id: str
) -> ProposalOut:
    """Reject a PENDING proposal. Changes nothing else; rejecting twice is fine."""
    pid = _proposal_id(proposal_id)
    sm = request.app.state.sessionmaker
    await svc.reject(sm, request.app.state.settings, access.user_id, access.room_id, pid)
    _pid, view, perms = await _visible(request, access, proposal_id)
    return _out(request.app.state.settings, access, view, perms)


@router.post(
    "/rooms/{room_id}/ai-proposals/{proposal_id}/regenerate",
    response_model=RequestOut,
    status_code=201,
)
async def regenerate_proposal(
    request: Request, access: MemberAccessUnsafe, proposal_id: str
) -> RequestOut:
    """After a conflict: ask again, from the document's CURRENT content, with the same instruction, files and
    target blocks. A new request, linked to the old proposal, which stays readable and becomes superseded."""
    body = await parse_json_body(request, RegenerateIn)
    view = await svc.regenerate(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        _proposal_id(proposal_id),
        body.client_request_id,
    )
    return request_out(request.app.state.settings, view, access)


@router.post(
    "/rooms/{room_id}/ai-proposals/{proposal_id}/publish",
    response_model=PublishOut,
    responses={
        403: {"model": ErrorBody, "description": "insufficient_role (owner only)"},
        409: {
            "model": ErrorBody,
            "description": "proposal_* / document_limit_reached / document_request_id_reused",
        },
    },
)
async def publish_draft(
    request: Request, access: MemberAccessUnsafe, proposal_id: str
) -> PublishOut:
    """Owner only. Publish a DRAFT as a new document through the existing create-document path (events, outbox
    and limits included). The new document becomes the room's ACTIVE document. A retry with the same
    `client_request_id` creates nothing twice."""
    if access.role != "owner":
        raise insufficient_role()
    body = await parse_json_body(request, PublishIn)
    document, created = await svc.publish_draft(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        _proposal_id(proposal_id),
        body.client_request_id,
    )
    return PublishOut(
        document_id=document.id, title=document.title, created=created, is_active=document.is_active
    )


__all__ = ["AppError", "requests_svc", "router"]
