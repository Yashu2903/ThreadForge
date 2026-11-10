"""Edit proposals and drafts: routes (AI phase 2).

Gate order as everywhere: authentication (401) -> CSRF on unsafe methods (403) -> room access (404 for
non-members) -> the proposal exists in THIS room (404 ai_proposal_not_found, whatever the reason: a proposal of
another room and a missing one are indistinguishable) -> who may see it (403).

WHO MAY SEE A PROPOSAL, ITS PREVIEW AND ITS CONTEXT: the requester, and any owner or editor with current write
access to the target document; for a draft, the requester and the owner(s). A viewer sees only the ordinary
chat message. Reading never changes anything and never calls a provider.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.dependencies.rooms import MemberAccess, RoomAccess, parse_uuid
from app.errors import ai_proposal_not_found, ai_write_access_required, insufficient_role
from app.schemas.auth import ErrorBody
from app.services import ai_proposals as svc
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


def _proposal_id(raw: str) -> uuid.UUID:
    parsed = parse_uuid(raw)
    if parsed is None:
        raise ai_proposal_not_found()
    return parsed


def _out(settings: Any, access: RoomAccess, view: svc.ProposalRow) -> ProposalOut:
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
    )


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
    pid = _proposal_id(proposal_id)
    sm = request.app.state.sessionmaker
    settings = request.app.state.settings
    view = await svc.get_proposal(sm, access.room_id, pid)
    if view is None:
        raise ai_proposal_not_found()
    if access.role not in EDIT_ROLES:
        raise insufficient_role()
    async with sm() as db:
        allowed = await svc.may_view(
            db, settings, role=access.role, user_id=access.user_id, row=view.row
        )
    if not allowed:
        raise ai_write_access_required()
    return _out(settings, access, view)
