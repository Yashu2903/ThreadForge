"""Analysis routes (AI phase 1, checkpoint 3): ask for a source version to be analyzed, cancel, see
its status, browse its extracted passages.

Gate order as everywhere: authentication (401) -> CSRF on unsafe methods (403) -> room access (404 for
non-members) -> role (403 insufficient_role) -> body validation (422) -> the service, which re-checks
the role and the whole AI gate under the room lock. These routes never call a provider and never read
a file: they write a job row (or cancel one) and read results.

Who may do what (docs/ai.md):
  analyze / cancel      owner or editor with can_write on the anchor document
  status                any member (counts and warnings; no text)
  passages              owner or editor (the extracted text itself)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.dependencies.rooms import MemberAccess, MemberAccessUnsafe, RoomAccess, parse_uuid
from app.errors import insufficient_role, source_not_found, source_version_not_found
from app.request_body import json_body_docs, parse_json_body
from app.schemas.auth import ErrorBody
from app.services import analysis as svc

router = APIRouter(tags=["analysis"])
BASE = "/rooms/{room_id}/sources/{source_id}/versions/{version_id}"


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # strict=False: JSON carries ids as strings; every other field stays strictly typed
    document_id: uuid.UUID = Field(strict=False)


class EstimateOut(BaseModel):
    embed_tokens: int
    vision_pages: int
    figures: int
    calls: int
    cost_known: bool
    expected_cost_micros: int | None
    max_cost_micros: int | None


class AnalyzeOut(BaseModel):
    status: str  # ready | created | joined
    job_id: uuid.UUID | None
    generation_id: uuid.UUID | None
    key_source_generation: str
    key_source_embedding: str
    estimate: EstimateOut


class AnalysisOut(BaseModel):
    version_id: uuid.UUID
    analysis_state: str
    error_code: str | None
    job_state: str | None
    job_stage: str | None
    job_error_code: str | None
    attempts: int | None
    waiting: int
    generation_id: uuid.UUID | None
    chunk_count: int | None
    figure_count: int | None
    figures_found: int | None
    pages_total: int | None
    pages_unreadable: int | None
    warnings: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    embed_model: str | None
    published_at: datetime | None


class PassageOut(BaseModel):
    id: uuid.UUID
    ordinal: int
    kind: str
    text: str
    extraction: str
    ocr_confidence: float | None
    locator: dict[str, Any]
    flags: dict[str, Any]
    section_key: str | None
    token_count: int
    figure_number: int | None


class PassagesOut(BaseModel):
    passages: list[PassageOut]
    next_after: int | None


def _require_editor(access: RoomAccess) -> None:
    if access.role not in svc.EDIT_ROLES:
        raise insufficient_role()


def _ids(source_id: str, version_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    source = parse_uuid(source_id)
    version = parse_uuid(version_id)
    if source is None:
        raise source_not_found()
    if version is None:
        raise source_version_not_found()
    return source, version


@router.post(
    BASE + "/analyze",
    response_model=AnalyzeOut,
    status_code=202,
    responses={
        401: {"model": ErrorBody},
        403: {
            "model": ErrorBody,
            "description": "csrf_failed / insufficient_role / consent_required / ai_write_access_required",
        },
        404: {
            "model": ErrorBody,
            "description": "room_not_found / source_not_found / document_not_found",
        },
        409: {
            "model": ErrorBody,
            "description": "ai_disabled_for_room / ai_anchor_required / key_unavailable / key_rejected / analysis_cap_exceeded",
        },
        503: {
            "model": ErrorBody,
            "description": "ai_not_configured / ai_disabled / ai_unavailable",
        },
    },
    openapi_extra=json_body_docs(AnalyzeRequest),
)
async def analyze(
    request: Request, access: MemberAccessUnsafe, source_id: str, version_id: str
) -> AnalyzeOut:
    """ "Analyze now". Owner or editor with write access to the named document. Reuses a ready index
    (no job), joins a job already queued or running for this file, or creates one. Nothing is read or
    sent to a provider by this request: the AI worker does that, later."""
    _require_editor(access)
    source, version = _ids(source_id, version_id)
    body = await parse_json_body(request, AnalyzeRequest)
    admitted = await svc.request_analysis(
        request.app.state.sessionmaker,
        request.app.state.settings,
        access.user_id,
        access.room_id,
        source,
        version,
        body.document_id,
    )
    return AnalyzeOut(
        status=admitted.status,
        job_id=admitted.job_id,
        generation_id=admitted.generation_id,
        key_source_generation=admitted.key_source_generation,
        key_source_embedding=admitted.key_source_embedding,
        estimate=EstimateOut(**vars(admitted.estimate)),
    )


@router.delete(
    BASE + "/analyze",
    status_code=204,
    responses={
        404: {"model": ErrorBody, "description": "room_not_found / source_version_not_found"}
    },
)
async def cancel_analysis(
    request: Request, access: MemberAccessUnsafe, source_id: str, version_id: str
) -> Response:
    """Withdraw YOUR "Analyze now". The job is cancelled only if nobody else (another person, or a
    chat request) is waiting for it."""
    _require_editor(access)
    _source, version = _ids(source_id, version_id)
    await svc.cancel_manual(request.app.state.sessionmaker, access.user_id, access.room_id, version)
    return Response(status_code=204)


@router.get(
    BASE + "/analysis",
    response_model=AnalysisOut,
    responses={
        404: {"model": ErrorBody, "description": "room_not_found / source_version_not_found"}
    },
)
async def get_analysis(
    request: Request, access: MemberAccess, source_id: str, version_id: str
) -> AnalysisOut:
    """Any member: where the analysis stands (state, stage, counts, warnings, gaps). No text."""
    _source, version = _ids(source_id, version_id)
    view = await svc.get_analysis(request.app.state.sessionmaker, access.room_id, version)
    return AnalysisOut(**vars(view))


@router.get(
    BASE + "/passages",
    response_model=PassagesOut,
    responses={
        403: {"model": ErrorBody, "description": "insufficient_role (viewers)"},
        404: {"model": ErrorBody, "description": "room_not_found / source_version_not_found"},
    },
)
async def list_passages(
    request: Request,
    access: MemberAccess,
    source_id: str,
    version_id: str,
    after: int = -1,
    limit: int = 50,
    q: str | None = None,
) -> PassagesOut:
    """Owner or editor: the extracted passages of the version's current index, in reading order.
    `q` narrows them by full-text search (the lexical half of retrieval). Page with `after`."""
    _require_editor(access)
    _source, version = _ids(source_id, version_id)
    limit = max(1, min(limit, 200))
    query = q.strip()[:200] if q and q.strip() else None
    rows, next_after = await svc.list_passages(
        request.app.state.sessionmaker,
        access.room_id,
        version,
        after=max(after, -1),
        limit=limit,
        query=query,
    )
    return PassagesOut(passages=[PassageOut(**vars(r)) for r in rows], next_after=next_after)
