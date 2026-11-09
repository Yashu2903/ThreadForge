"""ThreadForge AI routes (checkpoint 1): status, consent, personal keys, the room switch, the usage
page and the estimate. Gate order is the same everywhere: authentication (401), CSRF on unsafe
methods (403), room access (404/403), body validation (422), then the service."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, Request, Response

from app.ai import access, keys
from app.ai.estimate import MAX_CHAT_CONTEXT, estimate_request
from app.ai.notice import NOTICE_VERSION, notice_points
from app.ai.status import load_status
from app.ai.usage import usage_for_room
from app.dependencies.auth import CsrfProtectedUser, CurrentUser
from app.dependencies.rooms import MemberAccess, MemberAccessUnsafe, OwnerAccess, OwnerAccessUnsafe
from app.errors import AppError, document_not_found, validation_failed
from app.models import AiConsent, Room
from app.schemas.ai import (
    ConsentRequest,
    EstimateRequest,
    RoomAiSettingsRequest,
    SaveKeyRequest,
)
from app.services import rooms as room_service

router = APIRouter(tags=["ai"])
_CONSENTS = AiConsent.__table__
_ROOMS = Room.__table__


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _meta(m: keys.KeyMeta) -> dict[str, Any]:
    return {
        "provider": m.provider,
        "last4": m.last4,
        "state": m.state,
        "created_at": m.created_at.isoformat(),
        "last_validated_at": m.last_validated_at.isoformat() if m.last_validated_at else None,
    }


def _choice(c: keys.KeyChoice) -> dict[str, Any]:
    return {"provider": c.provider, "source": c.source, "problem": c.problem}


# ---- status ----------------------------------------------------------------------------------


@router.get("/ai/status")
async def ai_status(request: Request, user: CurrentUser) -> dict[str, Any]:
    """Booleans and codes only: no secrets, no model prompts, no other user's data."""
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as db:
        status = await load_status(db, settings)
    return {
        "available": status.available,
        "code": status.code,
        "configured": status.configured,
        "globally_enabled": status.globally_enabled,
        "key_policy": settings.ai_key_policy,
        "notice_version": NOTICE_VERSION,
        "spend_controls": {
            "prices_configured": settings.ai_prices_configured,
            "budgets_enforced": settings.ai_prices_configured
            and any(
                b is not None
                for b in (
                    settings.ai_budget_global_monthly_micros,
                    settings.ai_budget_user_daily_micros,
                    settings.ai_budget_room_daily_micros,
                )
            ),
        },
        "capabilities": {
            name: {
                "state": cap.state,
                "code": cap.code,
                "checked_at": cap.checked_at.isoformat() if cap.checked_at else None,
            }
            for name, cap in status.capabilities.items()
        },
    }


# ---- consent ---------------------------------------------------------------------------------


@router.get("/ai/consent")
async def get_consent(request: Request, user: CurrentUser) -> dict[str, Any]:
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as db:
        accepted = await access.has_consent(db, user.user.id)
        choices = {
            "generation": await keys.choose_key_source(db, settings, user.user.id, "openai"),
            "embedding": await keys.choose_key_source(db, settings, user.user.id, "google"),
        }
    return {
        "notice_version": NOTICE_VERSION,
        "accepted": accepted,
        "points": notice_points(embed_provider="Google", llm_provider="OpenAI"),
        "key_sources": {k: _choice(v) for k, v in choices.items()},
    }


@router.post("/ai/consent", status_code=204)
async def accept_consent(
    body: ConsentRequest, request: Request, user: CsrfProtectedUser
) -> Response:
    if body.notice_version != NOTICE_VERSION:
        raise AppError(409, "notice_outdated", "The AI notice changed; reload and read it again.")
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    async with request.app.state.sessionmaker() as db, db.begin():
        await db.execute(
            pg_insert(_CONSENTS)
            .values(user_id=user.user.id, notice_version=NOTICE_VERSION)
            .on_conflict_do_nothing()
        )
    return Response(status_code=204)


# ---- personal keys ---------------------------------------------------------------------------


@router.get("/ai/keys")
async def list_my_keys(request: Request, user: CurrentUser) -> dict[str, Any]:
    items = await keys.list_keys(request.app.state.sessionmaker, user.user.id)
    return {"items": [_meta(m) for m in items]}


@router.put("/ai/keys/{provider}")
async def save_my_key(
    provider: str, body: SaveKeyRequest, request: Request, user: CsrfProtectedUser
) -> dict[str, Any]:
    meta = await keys.save_key(
        request.app.state.sessionmaker,
        request.app.state.settings,
        user.user.id,
        provider,
        body.api_key,
        _client_ip(request),
        transports=getattr(request.app.state, "ai_transports", None),
    )
    return _meta(meta)


@router.delete("/ai/keys/{provider}", status_code=204)
async def delete_my_key(provider: str, request: Request, user: CsrfProtectedUser) -> Response:
    if provider not in keys.PROVIDERS:
        raise validation_failed("Unknown provider.")
    await keys.delete_key(request.app.state.sessionmaker, user.user.id, provider)
    return Response(status_code=204)


# ---- the room switch and usage ----------------------------------------------------------------


async def _room_ai_view(request: Request, room_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import select  # noqa: PLC0415

    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as db:
        status = await load_status(db, settings)
        room_on = (
            await db.execute(select(_ROOMS.c.ai_enabled).where(_ROOMS.c.id == room_id))
        ).scalar_one()
    return {
        "ai_enabled": bool(room_on),
        "available": status.available and bool(room_on),
        "code": status.code
        if status.code != "ok"
        else ("ok" if room_on else "ai_disabled_for_room"),
    }


@router.get("/rooms/{room_id}/ai-settings")
async def get_room_ai_settings(request: Request, access_: MemberAccess) -> dict[str, Any]:
    return await _room_ai_view(request, access_.room_id)


@router.put("/rooms/{room_id}/ai-settings")
async def put_room_ai_settings(
    body: RoomAiSettingsRequest, request: Request, access_: OwnerAccessUnsafe
) -> dict[str, Any]:
    await room_service.set_room_ai_enabled(
        request.app.state.sessionmaker, access_.user_id, access_.room_id, body.ai_enabled
    )
    return await _room_ai_view(request, access_.room_id)


@router.get("/rooms/{room_id}/ai-usage")
async def get_room_ai_usage(
    request: Request, access_: OwnerAccess, days: int = Query(30, ge=1, le=365)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        return await usage_for_room(db, request.app.state.settings, access_.room_id, days)


# ---- estimate --------------------------------------------------------------------------------


@router.post("/rooms/{room_id}/ai-requests/estimate")
async def estimate(
    body: EstimateRequest, request: Request, access_: MemberAccessUnsafe
) -> dict[str, Any]:
    """Approximate size, calls and cost, and WHICH key would be charged. Runs the same gate as a
    real request (except consent, which it reports), so what the estimate says is what a send would
    be allowed to do."""
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as db:
        admission = await access.gate(
            db,
            settings,
            user_id=access_.user_id,
            role=access_.role,
            room_id=access_.room_id,
            document_id=body.document_id,
            require_consent=False,
        )
        if body.chat_message_ids and not body.include_chat_context:
            raise validation_failed("chat_message_ids needs include_chat_context.")
        if len(body.chat_message_ids) > MAX_CHAT_CONTEXT:
            raise validation_failed("Too many chat messages selected.")
        if body.document_scope != "none" and not body.include_document_content:
            raise validation_failed("document_scope needs include_document_content.")
        if body.document_id is None:
            raise document_not_found()
        est = await estimate_request(
            db,
            settings,
            room_id=access_.room_id,
            document_id=body.document_id,
            instruction=body.instruction,
            task_hint=body.task_hint,
            document_scope=body.document_scope,
            include_document_content=body.include_document_content,
            selected_chars=body.selected_chars,
            chat_message_ids=body.chat_message_ids if body.include_chat_context else [],
        )
        extra = {}
        if body.source_version_ids:
            from sqlalchemy import select as sa_select

            from app.models import SourceVersion
            from app.services import ai_requests as request_service
            from app.services import sources as source_service

            await source_service.resolve_versions(
                db, access_.room_id, list(body.source_version_ids)
            )
            versions = (
                await db.execute(
                    sa_select(SourceVersion.__table__).where(
                        SourceVersion.__table__.c.id.in_(body.source_version_ids)
                    )
                )
            ).all()
            est, extra, _pages, _figures = await request_service._estimate(
                db,
                settings,
                access_.room_id,
                request_service.RequestInput(
                    client_request_id=uuid.uuid4(),
                    instruction=body.instruction,
                    task_hint=body.task_hint,
                    document_id=body.document_id,
                    include_document_content=body.include_document_content,
                    document_scope=body.document_scope,
                    selected_block_ids=(),
                    selection_hash=None,
                    source_version_ids=tuple(body.source_version_ids),
                    include_chat_context=body.include_chat_context,
                    chat_message_ids=tuple(body.chat_message_ids)
                    if body.include_chat_context
                    else (),
                    confirm_large=False,
                ),
                list(versions),
            )
    usable = all(c.source is not None for c in admission.key_sources.values())
    blocking = next((c.problem for c in admission.key_sources.values() if c.problem), None)
    if est.over_caps:
        blocking = "caps_exceeded"
    return {
        "estimate": {
            "input_tokens": est.input_tokens,
            "expected_output_tokens": est.expected_output_tokens,
            "max_output_tokens": est.max_output_tokens,
            "provider_calls": est.calls,
            "images": est.images,
            "cost_known": est.cost_known,
            "expected_cost_micros": est.expected_cost_micros,
            "max_cost_micros": est.max_cost_micros,
            "requires_confirmation": est.requires_confirmation,
            "over_caps": est.over_caps,
            "new_analysis_pages": extra.get("new_analysis_pages", 0),
            "new_analysis_figures": extra.get("new_analysis_figures", 0),
        },
        "key_sources": {k: _choice(v) for k, v in admission.key_sources.items()},
        "consent_given": admission.consent_given,
        "can_send": usable and not est.over_caps and admission.consent_given,
        "blocking_code": blocking,
        "note": "Estimates are approximate. Files not yet analyzed add the cost of analyzing them.",
    }
