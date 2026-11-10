"""`@AI` requests: admission, the state machine, cancel, retry, and publication.

The ONLY module that writes `ai_requests`, `ai_request_sources`, `ai_results` and the AI chat messages.
The worker's model calls live in app/ai/answer/execute.py; every database effect they have goes through
here.

STATES (docs/ai.md section 13)
    waiting_for_sources -> queued -> running -> validating -> succeeded
                                        |            |-> failed / cancelled
    any non-final state -> cancelling (a worker is running) -> cancelled   | expired (waited too long)

LOCK ORDER everywhere: the ROOM row first, then the request row. A transition that changes a state also
records its `ai_request_updated` event in the same transaction, which needs the room lock; that is why
every function below that moves a request starts with `lock_room`. The worker's claim is the one
exception: it takes only the request row (it does not yet know the room, and it records no event); a
separate step records "running". Taking the request row first and the room second anywhere would
deadlock against the functions that take them in the documented order.

EXACTLY-ONE PUBLISHED ANSWER. `ai_results` is UNIQUE per request, and the reply message has a client
message id derived from the request id (author: the system user), which the messages table makes
unique. A worker that crashes and is replaced may recompute (and spend again); it cannot publish twice.

CONTENT. Instructions, answers and manifests are never logged: log lines carry ids, states and codes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy import null as sql_null
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import access
from app.ai.analysis import jobs
from app.ai.estimate import Estimate, estimate_analysis, estimate_request, needs_confirmation
from app.ai.pricing import Prices, estimate_text_tokens
from app.ai.types import Usage
from app.config import Settings
from app.errors import (
    ai_capacity,
    ai_context_required,
    ai_proposals_disabled,
    ai_request_id_reused,
    ai_request_not_cancellable,
    ai_request_not_found,
    ai_request_not_retryable,
    analysis_cap_exceeded,
    confirmation_required,
    edit_target_required,
    insufficient_role,
    invalid_ai_request,
    key_rejected,
    key_unavailable,
    proposal_rate_limited,
)
from app.logging_config import APP_LOGGER
from app.models import (
    AiManifest,
    AiProposal,
    AiRequest,
    AiRequestSource,
    AiResult,
    Source,
    SourceVersion,
    User,
)
from app.services import messages
from app.services import sources as source_service
from app.services.room_events import record_room_event
from app.services.room_lock import LockedRoom, lock_room
from app.services.rooms import lock_room_and_actor

log = logging.getLogger(f"{APP_LOGGER}.ai.requests")
_REQ = AiRequest.__table__
_RS = AiRequestSource.__table__
_RES = AiResult.__table__
_PROP = AiProposal.__table__
_MAN = AiManifest.__table__
_VER = SourceVersion.__table__
_SRC = Source.__table__
_USERS = User.__table__

SYSTEM_USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000a1a1a")
SYSTEM_USER_NAME = "ThreadForge AI"
REPLY_NAMESPACE = uuid.UUID("0c2b4c1e-6f0e-4a8d-9d57-5f1c7c9a2b10")
FINGERPRINT_VERSION = b"ai-request-v1"
EDIT_ROLES = ("owner", "editor")
ACTIVE = ("waiting_for_sources", "queued", "running", "validating", "cancelling")
TERMINAL = ("succeeded", "failed", "cancelled", "expired")
CLAIM_STATES = ("running", "validating", "cancelling")
MAX_CLAIMS = 5  # a request whose worker keeps dying is failed instead of retried forever
STAGE_LABELS = {
    "waiting_for_files": "Waiting for files",
    "analyzing_files": "Analyzing files",
    "gathering_context": "Gathering context",
    "generating": "Generating",
    "validating": "Validating",
}


def reply_client_message_id(request_id: uuid.UUID) -> uuid.UUID:
    """The AI reply's message id is a function of the request: one request, one reply, whatever the
    retries."""
    return uuid.uuid5(REPLY_NAMESPACE, str(request_id))


async def ensure_system_user(db: AsyncSession) -> None:
    """The author of AI replies: active, no auth identity, no sessions, no membership (so it never
    appears in member lists, presence or invitations). Upserted on use so a wiped table heals."""
    await db.execute(
        pg_insert(_USERS)
        .values(id=SYSTEM_USER_ID, display_name=SYSTEM_USER_NAME, state="active")
        .on_conflict_do_nothing(index_elements=["id"])
    )


# ---- inputs, views --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestInput:
    client_request_id: uuid.UUID
    instruction: str
    task_hint: str
    document_id: uuid.UUID | None
    include_document_content: bool
    document_scope: str
    selected_block_ids: tuple[uuid.UUID, ...]
    selection_hash: str | None
    source_version_ids: tuple[uuid.UUID, ...]
    include_chat_context: bool
    chat_message_ids: tuple[uuid.UUID, ...]
    confirm_large: bool
    # phase 2: "answer" | "edit" | "draft", and the edit target (ranges / insertion point)
    operation: str = "answer"
    selected_ranges: tuple[dict[str, Any], ...] = ()
    insert_at: str | None = None
    # the proposal this request regenerates: the blocks are re-read from the CURRENT document, not re-hashed
    regenerate_of: uuid.UUID | None = None


def compute_fingerprint(settings: Settings, actor_id: uuid.UUID, inp: RequestInput) -> bytes:
    """Keyed HMAC over a canonical form of everything that defines the request EXCEPT confirm_large
    (a request refused for confirmation was never created, so confirming it is the same request)."""
    canonical = json.dumps(
        {
            "actor": str(actor_id),
            "client_request_id": str(inp.client_request_id),
            "instruction": inp.instruction,
            "task_hint": inp.task_hint,
            "document_id": str(inp.document_id) if inp.document_id else None,
            "include_document_content": inp.include_document_content,
            "document_scope": inp.document_scope,
            "selected_block_ids": [str(i) for i in inp.selected_block_ids],
            "selection_hash": inp.selection_hash,
            "source_version_ids": [str(i) for i in inp.source_version_ids],
            "include_chat_context": inp.include_chat_context,
            "chat_message_ids": [str(i) for i in inp.chat_message_ids],
            # only for edit/draft, so the fingerprint of every phase 1 request is unchanged
            **(
                {
                    "operation": inp.operation,
                    "selected_ranges": list(inp.selected_ranges),
                    "insert_at": inp.insert_at,
                    "regenerate_of": str(inp.regenerate_of) if inp.regenerate_of else None,
                }
                if inp.operation != "answer"
                else {}
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    key = settings.message_fingerprint_secret.get_secret_value().encode("utf-8")
    return hmac.new(key, FINGERPRINT_VERSION + b"\n" + canonical, hashlib.sha256).digest()


@dataclass(frozen=True)
class SourceRef:
    version_id: uuid.UUID
    source_id: uuid.UUID
    name: str
    version_no: int
    analysis_state: str
    error_code: str | None
    kind: str


@dataclass(frozen=True)
class RequestView:
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
    error_detail: dict | None
    attempt: int
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    document_id: uuid.UUID | None
    include_document_content: bool
    document_scope: str
    include_chat_context: bool
    chat_message_count: int
    sources: tuple[SourceRef, ...]
    queue_position: int | None
    estimate: dict
    key_source_generation: str
    key_source_embedding: str
    cancel_requested: bool
    reply_message_id: uuid.UUID | None = None
    operation: str = "answer"
    proposal_id: uuid.UUID | None = None


# ---- reading ----------------------------------------------------------------------------------------------


async def _view(db: AsyncSession, row: Any) -> RequestView:
    refs = (
        await db.execute(
            text(
                """SELECT v.id AS version_id, v.source_id, s.display_name, v.version_no,
                          v.analysis_state, v.error_code, v.kind
                     FROM ai_request_sources rs
                     JOIN source_versions v ON v.id = rs.source_version_id AND v.room_id = rs.room_id
                     JOIN sources s ON s.id = v.source_id AND s.room_id = v.room_id
                    WHERE rs.request_id = :r ORDER BY rs.position"""
            ),
            {"r": row.id},
        )
    ).all()
    position = None
    if row.state == "queued":
        position = (
            await db.execute(
                text(
                    "SELECT count(*) + 1 FROM ai_requests WHERE state = 'queued' "
                    "AND (created_at, id) < (:c, :i)"
                ),
                {"c": row.created_at, "i": row.id},
            )
        ).scalar_one()
    reply = (
        await db.execute(select(_RES.c.reply_message_id).where(_RES.c.request_id == row.id))
    ).scalar_one_or_none()
    proposal_id = (
        await db.execute(select(_PROP.c.id).where(_PROP.c.request_id == row.id))
    ).scalar_one_or_none()
    sources = tuple(
        SourceRef(
            r.version_id,
            r.source_id,
            r.display_name,
            r.version_no,
            r.analysis_state,
            r.error_code,
            r.kind,
        )
        for r in refs
    )
    stage = row.stage
    if row.state == "waiting_for_sources":
        busy = any(s.analysis_state in ("queued", "extracting", "indexing") for s in sources)
        stage = (
            "analyzing_files"
            if busy and any(s.analysis_state in ("extracting", "indexing") for s in sources)
            else "waiting_for_files"
        )
    elif row.state == "validating":
        stage = "validating"
    return RequestView(
        id=row.id,
        room_id=row.room_id,
        message_id=row.message_id,
        requested_by=row.requested_by,
        state=row.state,
        stage=stage,
        stage_label=STAGE_LABELS.get(stage or ""),
        task_hint=row.task_hint,
        task_type=row.task_type,
        error_code=row.error_code,
        error_detail=row.error_detail,
        attempt=row.attempt,
        created_at=row.created_at,
        queued_at=row.queued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        document_id=row.document_id,
        include_document_content=row.include_document_content,
        document_scope=row.document_scope,
        include_chat_context=row.include_chat_context,
        chat_message_count=len(row.chat_message_ids or []),
        sources=sources,
        queue_position=position,
        estimate=row.estimate or {},
        key_source_generation=row.key_source_generation,
        key_source_embedding=row.key_source_embedding,
        cancel_requested=row.cancel_requested_at is not None and row.state not in TERMINAL,
        reply_message_id=reply,
        operation=row.operation,
        proposal_id=proposal_id,
    )


async def get_request(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, request_id: uuid.UUID
) -> RequestView | None:
    async with sessionmaker() as db:
        row = (
            await db.execute(select(_REQ).where(_REQ.c.id == request_id, _REQ.c.room_id == room_id))
        ).one_or_none()
        return await _view(db, row) if row else None


async def get_request_row(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, request_id: uuid.UUID
) -> Any:
    async with sessionmaker() as db:
        return (
            await db.execute(select(_REQ).where(_REQ.c.id == request_id, _REQ.c.room_id == room_id))
        ).one_or_none()


# ---- events ------------------------------------------------------------------------------------------------


async def _emit(
    db: AsyncSession, locked: LockedRoom, request_id: uuid.UUID, state: str, actor: uuid.UUID
) -> None:
    await record_room_event(
        db,
        locked,
        event_type="ai_request_updated",
        resource_type="ai_request",
        resource_id=request_id,
        resource_version=None,
        actor_id=actor,
        payload={"request_id": str(request_id), "state": state},
    )


# ---- admission ---------------------------------------------------------------------------------------------


@dataclass
class _Plan:
    estimate: Estimate
    analysis_pages: int
    analysis_figures: int


async def _estimate(
    db: AsyncSession, settings: Settings, room_id: uuid.UUID, inp: RequestInput, versions: list[Any]
) -> tuple[Estimate, dict, int, int]:
    """Approximate tokens, calls and cost for the whole request: the chat/document part from the
    checkpoint-1 estimator, plus evidence from the chosen files and the analysis they would trigger."""
    selected_chars = 0
    scope = "blocks" if inp.document_scope == "edit" else inp.document_scope
    if scope == "blocks":
        # blocks are unknown until capture
        selected_chars = 400 * max(1, len(inp.selected_block_ids) + len(inp.selected_ranges))
    base = await estimate_request(
        db,
        settings,
        room_id=room_id,
        document_id=inp.document_id,
        instruction=inp.instruction,
        task_hint=inp.task_hint,
        document_scope=scope,
        include_document_content=inp.include_document_content,
        selected_chars=selected_chars,
        chat_message_ids=list(inp.chat_message_ids),
    )
    prices = Prices.of(settings)
    text_tokens = sum(estimate_text_tokens(settings, "x" * v.text_chars) for v in versions)
    unanalyzed = [
        v for v in versions if not (v.analysis_state == "ready" and v.current_generation_id)
    ]
    pages = sum(v.page_count or 0 for v in unanalyzed)
    figures = sum(min(v.image_count, settings.ai_max_figures_per_source) for v in unanalyzed)
    evidence = 0
    extra_calls = 0
    if versions:
        if inp.task_hint == "summarize":
            evidence = min(text_tokens, len(versions) * settings.ai_summarize_source_tokens)
            extra_calls = -(-evidence // settings.ai_summarize_section_tokens) + 1
        else:
            evidence = min(text_tokens, settings.ai_retrieval_evidence_tokens)
            extra_calls = 1  # embedding the query
    input_tokens = base.input_tokens + evidence
    calls = base.calls + extra_calls
    expected = base.expected_cost_micros
    worst = base.max_cost_micros
    if prices.llm_known and expected is not None and worst is not None:
        more = prices.llm_cost(Usage(evidence, 0))
        expected += more
        worst += more
    analysis_calls = 0
    for v in unanalyzed:
        a = estimate_analysis(
            settings,
            text_chars=v.text_chars,
            likely_scanned_pages=v.likely_scanned_pages,
            image_count=v.image_count,
            kind=v.kind,
        )
        analysis_calls += a.calls
        if a.cost_known and a.expected_cost_micros is not None and expected is not None:
            expected += a.expected_cost_micros
            worst = (worst or 0) + (a.max_cost_micros or a.expected_cost_micros)
    over = list(base.over_caps)
    if (
        input_tokens > settings.ai_max_input_tokens
        and inp.task_hint != "summarize"
        and not versions
    ):
        over.append("max_input_tokens")
    est = Estimate(
        input_tokens=input_tokens,
        expected_output_tokens=base.expected_output_tokens,
        max_output_tokens=base.max_output_tokens,
        calls=calls + analysis_calls,
        images=min(settings.ai_retrieval_figure_top_k, settings.ai_max_images) if versions else 0,
        cost_known=base.cost_known,
        expected_cost_micros=expected,
        max_cost_micros=worst,
        requires_confirmation=False,
        over_caps=over,
    )
    est.requires_confirmation = needs_confirmation(settings, est)
    packed = {
        "input_tokens": est.input_tokens,
        "expected_output_tokens": est.expected_output_tokens,
        "calls": est.calls,
        "images": est.images,
        "cost_known": est.cost_known,
        "expected_cost_micros": est.expected_cost_micros,
        "max_cost_micros": est.max_cost_micros,
        "requires_confirmation": est.requires_confirmation,
        "new_analysis_pages": pages,
        "new_analysis_figures": figures,
    }
    return est, packed, pages, figures


def _key_error(problem: str | None, provider: str):
    return key_rejected(provider) if problem == "key_rejected" else key_unavailable(provider)


def validate_shape(settings: Settings, inp: RequestInput) -> None:
    """Pure checks of the request's own fields (the route's schema already checked types/lengths)."""
    if len(inp.source_version_ids) > settings.ai_request_max_sources:
        raise invalid_ai_request("too_many_sources", "Too many files selected.")
    if len(set(inp.source_version_ids)) != len(inp.source_version_ids):
        raise invalid_ai_request("duplicate_sources", "A file was selected twice.")
    if len(inp.chat_message_ids) > settings.ai_request_max_chat_context:
        raise invalid_ai_request("too_many_chat_messages", "Too many chat messages selected.")
    if len(set(inp.chat_message_ids)) != len(inp.chat_message_ids):
        raise invalid_ai_request("duplicate_chat_messages", "A chat message was selected twice.")
    if inp.include_chat_context != bool(inp.chat_message_ids):
        raise invalid_ai_request(
            "chat_context_mismatch", "Chat context needs explicit message ids, and only then."
        )
    if inp.operation != "answer":
        _validate_proposal_shape(inp)
        return
    if inp.selected_ranges or inp.insert_at:
        raise invalid_ai_request("edit_target", "An edit target needs an edit request.")
    if inp.include_document_content:
        if inp.document_scope not in ("whole", "blocks"):
            raise invalid_ai_request(
                "document_scope", "Choose the whole document or selected blocks."
            )
    elif inp.document_scope != "none":
        raise invalid_ai_request("document_scope", "Document scope needs include_document_content.")
    if inp.document_scope == "blocks":
        if not 1 <= len(inp.selected_block_ids) <= 50:
            raise invalid_ai_request("selected_blocks", "Select between 1 and 50 blocks.")
        if not inp.selection_hash:
            raise invalid_ai_request("selection_hash", "The selection needs its hash.")
    elif inp.selected_block_ids or inp.selection_hash:
        raise invalid_ai_request(
            "selected_blocks", "Selected blocks need the blocks document scope."
        )
    # chat messages are context, never evidence (they cannot be cited), so they cannot be a request's
    # only material: without a file or the document no claim could be supported
    if not (inp.source_version_ids or inp.include_document_content):
        raise ai_context_required()


def _validate_proposal_shape(inp: RequestInput) -> None:
    """Edit and draft requests. An EDIT needs a document and an explicit target (blocks, ranges, or an
    insertion point): with none, the person is asked to select, nothing is guessed. A DRAFT is anchored on the
    room role, has no document and no selection, and needs files to draft from."""
    if inp.operation not in ("edit", "draft"):
        raise invalid_ai_request("operation", "Unknown operation.")
    if inp.include_chat_context != bool(inp.chat_message_ids):
        raise invalid_ai_request(
            "chat_context_mismatch", "Chat context needs explicit message ids, and only then."
        )
    if inp.operation == "draft":
        if (
            inp.document_id is not None
            or inp.include_document_content
            or inp.document_scope != "none"
            or inp.selected_block_ids
            or inp.selected_ranges
            or inp.insert_at
            or inp.selection_hash
        ):
            raise invalid_ai_request("draft_shape", "A draft has no document and no selection.")
        if not inp.source_version_ids:
            raise ai_context_required()
        return
    if inp.document_id is None or not inp.include_document_content or inp.document_scope != "edit":
        raise invalid_ai_request("edit_shape", "An edit request targets one document.")
    if not (inp.selected_block_ids or inp.selected_ranges or inp.insert_at):
        raise edit_target_required()
    if inp.selected_block_ids and not inp.selection_hash and inp.regenerate_of is None:
        raise invalid_ai_request("selection_hash", "The selection needs its hash.")
    if inp.selection_hash and not inp.selected_block_ids:
        raise invalid_ai_request("selected_blocks", "A selection hash needs selected blocks.")
    if inp.insert_at not in (None, "start", "end"):
        raise invalid_ai_request("insert_at", "Insert at the start or the end.")


async def create_request(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    inp: RequestInput,
) -> tuple[RequestView, bool]:
    """Admit a request. ONE transaction under the room lock; the chat message, the request and its
    source links commit together or not at all. Returns (view, created)."""
    if inp.operation != "answer" and not settings.ai_proposals_enabled:
        raise ai_proposals_disabled()
    validate_shape(settings, inp)
    fingerprint = compute_fingerprint(settings, actor_id, inp)
    async with sessionmaker() as db, db.begin():
        locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        if role not in EDIT_ROLES:
            raise insufficient_role()
        existing = (
            await db.execute(
                select(_REQ).where(
                    _REQ.c.room_id == room_id,
                    _REQ.c.requested_by == actor_id,
                    _REQ.c.client_request_id == inp.client_request_id,
                )
            )
        ).one_or_none()
        if existing is not None:
            if not hmac.compare_digest(bytes(existing.request_fingerprint), fingerprint):
                raise ai_request_id_reused()
            return await _view(db, existing), False

        admission = await access.gate(
            db,
            settings,
            user_id=actor_id,
            role=role,
            room_id=room_id,
            document_id=inp.document_id,
            require_anchor=inp.operation != "draft",
        )
        if inp.operation != "answer":
            recent = (
                await db.execute(
                    select(func.count())
                    .select_from(_REQ)
                    .where(
                        _REQ.c.requested_by == actor_id,
                        _REQ.c.operation != "answer",
                        _REQ.c.created_at > func.now() - timedelta(hours=1),
                    )
                )
            ).scalar_one()
            if recent >= settings.ai_proposals_per_user_per_hour:
                raise proposal_rate_limited(settings.ai_proposals_per_user_per_hour)
        gen, emb = admission.key_sources["generation"], admission.key_sources["embedding"]
        if gen.source is None:
            raise _key_error(gen.problem, "openai")
        if inp.source_version_ids and emb.source is None:
            raise _key_error(emb.problem, "google")
        emb_source = emb.source or gen.source

        # capacity: per user and per room, counted under the room lock so parallel sends cannot slip past
        for scope, limit, column, value in (
            ("user", settings.ai_max_active_requests_per_user, _REQ.c.requested_by, actor_id),
            ("room", settings.ai_max_active_requests_per_room, _REQ.c.room_id, room_id),
        ):
            active = (
                await db.execute(
                    select(func.count())
                    .select_from(_REQ)
                    .where(column == value, _REQ.c.state.in_(ACTIVE))
                )
            ).scalar_one()
            if active >= limit:
                raise ai_capacity(scope, limit)

        # the pinned versions: this room, not removed, source active (source_unavailable otherwise)
        versions: list[Any] = []
        if inp.source_version_ids:
            await source_service.resolve_versions(db, room_id, list(inp.source_version_ids))
            rows = (
                await db.execute(
                    select(_VER).where(_VER.c.id.in_(inp.source_version_ids)).with_for_update()
                )
            ).all()
            order = {v: i for i, v in enumerate(inp.source_version_ids)}
            versions = sorted(rows, key=lambda r: order[r.id])

        # chat context: this room's live, human (kind=user or an AI request) messages only
        if inp.chat_message_ids:
            found = await messages.live_ids(
                db, room_id, list(inp.chat_message_ids), exclude_kind="ai_answer"
            )
            if len(found) != len(inp.chat_message_ids):
                raise invalid_ai_request(
                    "chat_messages",
                    "A selected chat message does not exist, was deleted, or is an AI answer.",
                )

        est, packed, pages, figures = await _estimate(db, settings, room_id, inp, versions)
        if pages > settings.ai_max_new_analysis_pages:
            raise analysis_cap_exceeded(
                "max_new_analysis_pages", settings.ai_max_new_analysis_pages, pages
            )
        if figures > settings.ai_max_new_analysis_figures:
            raise analysis_cap_exceeded(
                "max_new_analysis_figures", settings.ai_max_new_analysis_figures, figures
            )
        if est.requires_confirmation and not inp.confirm_large:
            raise confirmation_required(packed)

        # the message (through the message service's own helpers: sequence, event, outbox, rate limit)
        request_id = uuid.uuid4()
        await messages.consume_write_capacity(db, settings, room_id, actor_id)
        message_id = await messages.insert_message_row(
            db,
            locked,
            room_id=room_id,
            author_id=actor_id,
            client_message_id=inp.client_request_id,
            body="@AI " + inp.instruction,
            fingerprint=messages.compute_fingerprint(
                settings, inp.client_request_id, "@AI " + inp.instruction
            ),
            kind="ai_request",
            ai_request_id=request_id,
        )

        ready_all = True
        for v in versions:
            result = await jobs.enqueue(
                db,
                settings,
                room_id=room_id,
                version_id=v.id,
                requested_by=actor_id,
                anchor_document_id=inp.document_id,
                key_source_generation=gen.source,
                key_source_embedding=emb_source,
                waiter_id=request_id,
                waiter_kind="request",
            )
            ready_all = ready_all and result.status == "ready"
        state = "queued" if ready_all else "waiting_for_sources"
        await db.execute(
            _REQ.insert().values(
                id=request_id,
                room_id=room_id,
                requested_by=actor_id,
                message_id=message_id,
                client_request_id=inp.client_request_id,
                request_fingerprint=fingerprint,
                task_hint=inp.task_hint,
                operation=inp.operation,
                selected_ranges=list(inp.selected_ranges),
                insert_at=inp.insert_at,
                regenerate_of=inp.regenerate_of,
                instruction=inp.instruction,
                document_id=inp.document_id,
                include_document_content=inp.include_document_content,
                document_scope=inp.document_scope,
                selected_block_ids=[str(i) for i in inp.selected_block_ids],
                selection_hash=inp.selection_hash,
                include_chat_context=inp.include_chat_context,
                chat_message_ids=[str(i) for i in inp.chat_message_ids],
                confirm_large=inp.confirm_large,
                estimate=packed,
                state=state,
                key_source_generation=gen.source,
                key_source_embedding=emb_source,
                charged_user_id=actor_id,
                queued_at=func.now() if state == "queued" else None,
            )
        )
        for position, v in enumerate(versions):
            await db.execute(
                _RS.insert().values(
                    request_id=request_id,
                    room_id=room_id,
                    source_version_id=v.id,
                    position=position,
                    generation_id=v.current_generation_id if ready_all else None,
                )
            )
        await _emit(db, locked, request_id, state, actor_id)
        row = (await db.execute(select(_REQ).where(_REQ.c.id == request_id))).one()
        view = await _view(db, row)
    log.info(
        "action=ai_request_created room_id=%s request_id=%s actor_id=%s state=%s sources=%s",
        room_id,
        request_id,
        actor_id,
        state,
        len(versions),
    )
    return view, True


# ---- cancel, retry, deletion ---------------------------------------------------------------------------------


async def release_waiter_in(db: AsyncSession, request_id: uuid.UUID) -> int:
    """In the caller's transaction: this request no longer waits for any analysis job; a job nobody
    else waits for is cancelled (queued: now; running: at its next stage boundary)."""
    return await jobs.release_waiter_in(db, request_id)


async def _cancel_locked(
    db: AsyncSession, locked: LockedRoom, row: Any, actor_id: uuid.UUID
) -> str:
    """Cancel one request whose row is locked. Returns the new state."""
    if row.state in ("waiting_for_sources", "queued"):
        await db.execute(
            update(_REQ)
            .where(_REQ.c.id == row.id)
            .values(
                state="cancelled",
                finished_at=func.now(),
                stage=None,
                cancel_requested_at=func.now(),
            )
        )
        await release_waiter_in(db, row.id)
        await _emit(db, locked, row.id, "cancelled", actor_id)
        return "cancelled"
    if row.state in ("running", "validating"):
        await db.execute(
            update(_REQ)
            .where(_REQ.c.id == row.id)
            .values(state="cancelling", cancel_requested_at=func.now())
        )
        await _emit(db, locked, row.id, "cancelling", actor_id)
        return "cancelling"
    return row.state


async def cancel_request(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    request_id: uuid.UUID,
) -> str:
    """The requester, or the owner (any request in the room). Others: 403."""
    async with sessionmaker() as db, db.begin():
        locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        row = (
            await db.execute(
                select(_REQ)
                .where(_REQ.c.id == request_id, _REQ.c.room_id == room_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ai_request_not_found()
        if not (role == "owner" or (role in EDIT_ROLES and row.requested_by == actor_id)):
            raise insufficient_role()
        if row.state in TERMINAL:
            raise ai_request_not_cancellable(row.state)
        if row.state == "cancelling":
            return "cancelling"
        return await _cancel_locked(db, locked, row, actor_id)


async def on_message_deleted(
    db: AsyncSession, locked: LockedRoom, kind: str, request_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """Called by messages.delete_message in its transaction (room lock held, message row locked).
    Deleting an `@AI` message cancels its unfinished request; deleting an AI answer (the owner only)
    redacts the stored answer, like a tombstone."""
    if kind == "ai_request":
        row = (
            await db.execute(select(_REQ).where(_REQ.c.id == request_id).with_for_update())
        ).one_or_none()
        if row is not None and row.state not in TERMINAL:
            await _cancel_locked(db, locked, row, actor_id)
    elif kind == "ai_answer":
        await redact_result(db, request_id)


async def redact_result(db: AsyncSession, request_id: uuid.UUID) -> None:
    """The answer, its citations' excerpts and quotes are wiped; the citation STUBS (source version,
    locator, 'redacted') stay so the record shows that an answer existed."""
    row = (
        await db.execute(select(_RES.c.id, _RES.c.citations).where(_RES.c.request_id == request_id))
    ).one_or_none()
    if row is None:
        return
    stubs = [
        {k: v for k, v in c.items() if k not in ("excerpt", "quotes", "source_name")}
        | {"redacted": True}
        for c in (row.citations or [])
    ]
    await db.execute(
        update(_RES)
        .where(_RES.c.id == row.id)
        .values(answer=sql_null(), citations=stubs, redacted_at=func.now())
    )
    await db.execute(
        update(_MAN)
        .where(_MAN.c.request_id == request_id)
        .values(data={"redacted": True}, redacted_at=func.now())
    )
    # The card that carried a proposal is gone: a pending proposal can no longer be applied, and what it
    # proposed is wiped once it is not pending (an APPLIED proposal keeps its record: the change is in the
    # document now). An `applying` one is left alone; its apply is in flight and finishes on its own.
    await db.execute(
        update(_PROP)
        .where(_PROP.c.request_id == request_id, _PROP.c.state == "pending")
        .values(state="rejected", resolved_at=func.now(), resolution_code="message_deleted")
    )
    await db.execute(
        text(
            """UPDATE ai_proposals
                  SET operations = CASE kind WHEN 'edit' THEN '[]'::jsonb ELSE '{}'::jsonb END,
                      before_content = '{}'::jsonb, preview = '{}'::jsonb, scope = '{}'::jsonb,
                      base_hashes = '{}'::jsonb, citations = '[]'::jsonb
                WHERE request_id = :r AND state IN ('rejected', 'conflicted', 'expired', 'superseded')"""
        ),
        {"r": request_id},
    )


async def retry_request(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    request_id: uuid.UUID,
) -> RequestView:
    """The requester retries a failed or expired request: same inputs, same pinned versions (nothing
    new is pinned), everything re-checked under the room lock (role, can_write, switches, consent, the
    keys decided at admission, the files still available). At most AI_REQUEST_MAX_ATTEMPTS attempts."""
    async with sessionmaker() as db, db.begin():
        locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
        row = (
            await db.execute(
                select(_REQ)
                .where(_REQ.c.id == request_id, _REQ.c.room_id == room_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ai_request_not_found()
        if row.requested_by != actor_id or role not in EDIT_ROLES:
            raise insufficient_role()
        if row.state not in ("failed", "expired", "cancelled"):
            raise ai_request_not_retryable("state")
        if row.attempt >= settings.ai_request_max_attempts:
            raise ai_request_not_retryable("attempts_exhausted")
        if await messages.is_deleted(db, row.message_id):
            raise ai_request_not_retryable("message_deleted")
        await access.gate(
            db,
            settings,
            user_id=actor_id,
            role=role,
            room_id=room_id,
            document_id=row.document_id,
            require_anchor=row.operation != "draft",
        )
        pins = (
            await db.execute(select(_RS).where(_RS.c.request_id == row.id).order_by(_RS.c.position))
        ).all()
        ids = [p.source_version_id for p in pins]
        versions: list[Any] = []
        if ids:
            await source_service.resolve_versions(
                db, room_id, ids
            )  # raises source_unavailable if removed
            versions = list(
                (await db.execute(select(_VER).where(_VER.c.id.in_(ids)).with_for_update())).all()
            )
        ready_all = True
        for v in versions:
            result = await jobs.enqueue(
                db,
                settings,
                room_id=room_id,
                version_id=v.id,
                requested_by=actor_id,
                anchor_document_id=row.document_id,
                key_source_generation=row.key_source_generation,
                key_source_embedding=row.key_source_embedding,
                waiter_id=row.id,
                waiter_kind="request",
            )
            ready_all = ready_all and result.status == "ready"
        state = "queued" if ready_all else "waiting_for_sources"
        await db.execute(
            update(_REQ)
            .where(_REQ.c.id == row.id)
            .values(
                state=state,
                attempt=row.attempt + 1,
                error_code=None,
                error_detail=sql_null(),
                finished_at=None,
                cancel_requested_at=None,
                claim_token=None,
                lease_expires_at=None,
                stage=None,
                run_after=func.now(),
                created_at=func.now(),  # the TTL and the queue position count from the retry
                queued_at=func.now() if state == "queued" else None,
                retain_until=None,
                claims=0,
            )
        )
        if ready_all:
            await db.execute(
                text(
                    "UPDATE ai_request_sources rs SET generation_id = v.current_generation_id "
                    "FROM source_versions v WHERE rs.request_id = :r AND v.id = rs.source_version_id"
                ),
                {"r": row.id},
            )
        else:
            await db.execute(
                update(_RS).where(_RS.c.request_id == row.id).values(generation_id=None)
            )
        await _emit(db, locked, row.id, state, actor_id)
        fresh = (await db.execute(select(_REQ).where(_REQ.c.id == row.id))).one()
        return await _view(db, fresh)


# ---- background: waiting, expiry --------------------------------------------------------------------------------


async def _fail_locked(
    db: AsyncSession,
    locked: LockedRoom,
    row: Any,
    code: str,
    detail: dict | None,
    settings: Settings,
) -> None:
    await db.execute(
        update(_REQ)
        .where(_REQ.c.id == row.id)
        .values(
            state="failed",
            error_code=code,
            error_detail=detail if detail is not None else sql_null(),
            finished_at=func.now(),
            claim_token=None,
            lease_expires_at=None,
            stage=None,
            retain_until=func.now() + timedelta(days=settings.ai_result_retention_days),
        )
    )
    await release_waiter_in(db, row.id)
    await _emit(db, locked, row.id, "failed", row.requested_by)


async def advance_waiting(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """Move requests waiting for their files forward: all pinned versions ready -> freeze each
    version's generation and queue; a pinned version removed or failed -> fail with a code that says
    which. Never skips a source, never swaps in a newer version. Returns requests changed."""
    async with sessionmaker() as db:
        candidates = list(
            (
                await db.execute(
                    text(
                        "SELECT id, room_id FROM ai_requests WHERE state = 'waiting_for_sources' "
                        "ORDER BY created_at LIMIT 100"
                    )
                )
            ).all()
        )
    changed = 0
    for cand in candidates:
        async with sessionmaker() as db, db.begin():
            locked = await lock_room(db, cand.room_id)
            if locked is None:
                continue
            row = (
                await db.execute(select(_REQ).where(_REQ.c.id == cand.id).with_for_update())
            ).one_or_none()
            if row is None or row.state != "waiting_for_sources":
                continue
            pins = (
                await db.execute(
                    text(
                        """SELECT rs.source_version_id AS vid, v.analysis_state, v.error_code,
                                  v.current_generation_id
                             FROM ai_request_sources rs JOIN source_versions v ON v.id = rs.source_version_id
                            WHERE rs.request_id = :r"""
                    ),
                    {"r": row.id},
                )
            ).all()
            removed = [str(p.vid) for p in pins if p.analysis_state == "removed"]
            failed = [
                {"source_version_id": str(p.vid), "error_code": p.error_code}
                for p in pins
                if p.analysis_state == "failed"
            ]
            if removed:
                await _fail_locked(
                    db, locked, row, "source_removed", {"source_version_ids": removed}, settings
                )
                changed += 1
            elif failed:
                await _fail_locked(db, locked, row, "source_failed", {"sources": failed}, settings)
                changed += 1
            elif all(p.analysis_state == "ready" and p.current_generation_id for p in pins):
                await db.execute(
                    text(
                        "UPDATE ai_request_sources rs SET generation_id = v.current_generation_id "
                        "FROM source_versions v WHERE rs.request_id = :r AND v.id = rs.source_version_id"
                    ),
                    {"r": row.id},
                )
                await db.execute(
                    update(_REQ)
                    .where(_REQ.c.id == row.id)
                    .values(state="queued", queued_at=func.now())
                )
                await release_waiter_in(db, row.id)
                await _emit(db, locked, row.id, "queued", row.requested_by)
                changed += 1
    return changed


async def expire_stale(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    """Waiting or queued longer than AI_REQUEST_TTL_MINUTES: expired (and its analysis released)."""
    async with sessionmaker() as db:
        candidates = list(
            (
                await db.execute(
                    text(
                        "SELECT id, room_id FROM ai_requests WHERE state IN ('waiting_for_sources', 'queued') "
                        "AND created_at < now() - make_interval(mins => :ttl) LIMIT 100"
                    ),
                    {"ttl": settings.ai_request_ttl_minutes},
                )
            ).all()
        )
    changed = 0
    for cand in candidates:
        async with sessionmaker() as db, db.begin():
            locked = await lock_room(db, cand.room_id)
            row = (
                await db.execute(select(_REQ).where(_REQ.c.id == cand.id).with_for_update())
            ).one_or_none()
            if locked is None or row is None or row.state not in ("waiting_for_sources", "queued"):
                continue
            await db.execute(
                update(_REQ)
                .where(_REQ.c.id == row.id)
                .values(
                    state="expired",
                    error_code="request_expired",
                    finished_at=func.now(),
                    stage=None,
                    retain_until=func.now() + timedelta(days=settings.ai_result_retention_days),
                )
            )
            await release_waiter_in(db, row.id)
            await _emit(db, locked, row.id, "expired", row.requested_by)
            changed += 1
    return changed


# ---- worker: claim, lease, finish ---------------------------------------------------------------------------------

_CLAIM = text(
    """
    UPDATE ai_requests SET state = 'running', claim_token = :token,
           lease_expires_at = now() + make_interval(secs => :lease),
           started_at = COALESCE(started_at, now()), stage = 'gathering_context', claims = claims + 1
     WHERE id = (
        SELECT r.id FROM ai_requests r
         WHERE r.claims < :max_claims
           AND (   (r.state = 'queued' AND r.run_after <= now() AND r.cancel_requested_at IS NULL)
                OR (r.state IN ('running', 'validating') AND r.lease_expires_at < now()
                    AND r.cancel_requested_at IS NULL))
         ORDER BY (SELECT count(*) FROM ai_requests x
                    WHERE x.room_id = r.room_id AND x.state IN ('running', 'validating')
                      AND x.lease_expires_at >= now()),
                  r.created_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1)
    RETURNING *
    """
)


async def claim_next(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[Any, uuid.UUID] | None:
    token = uuid.uuid4()
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(
                _CLAIM,
                {
                    "token": token,
                    "lease": settings.ai_request_lease_seconds,
                    "max_claims": MAX_CLAIMS,
                },
            )
        ).one_or_none()
    if row is None:
        return None
    # the event is a separate step (room lock first); the claim itself must not wait for the room
    async with sessionmaker() as db, db.begin():
        locked = await lock_room(db, row.room_id)
        if locked is not None:
            await _emit(db, locked, row.id, "running", row.requested_by)
    return row, token


async def sweep_abandoned(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """Requests whose worker died and that must not run again: claims used up -> failed; a requested
    cancellation -> cancelled."""
    async with sessionmaker() as db:
        stuck = list(
            (
                await db.execute(
                    text(
                        """SELECT id, room_id FROM ai_requests
                            WHERE state IN ('running', 'validating', 'cancelling') AND lease_expires_at < now()
                              AND (claims >= :m OR cancel_requested_at IS NOT NULL) LIMIT 100"""
                    ),
                    {"m": MAX_CLAIMS},
                )
            ).all()
        )
    changed = 0
    for cand in stuck:
        async with sessionmaker() as db, db.begin():
            locked = await lock_room(db, cand.room_id)
            row = (
                await db.execute(select(_REQ).where(_REQ.c.id == cand.id).with_for_update())
            ).one_or_none()
            if locked is None or row is None or row.state not in CLAIM_STATES:
                continue
            if row.cancel_requested_at is not None:
                await db.execute(
                    update(_REQ)
                    .where(_REQ.c.id == row.id)
                    .values(
                        state="cancelled",
                        claim_token=None,
                        lease_expires_at=None,
                        finished_at=func.now(),
                        stage=None,
                    )
                )
                await _emit(db, locked, row.id, "cancelled", row.requested_by)
            else:
                await _fail_locked(db, locked, row, "execution_attempts_exhausted", None, settings)
            changed += 1
    return changed


async def renew(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    request_id: uuid.UUID,
    token: uuid.UUID,
    *,
    stage: str | None = None,
    state: str | None = None,
) -> str | None:
    """Extend the lease and record the stage. None while the request is still ours and wanted, else
    `lost_claim` | `cancelled` (a cancellation was requested)."""
    async with sessionmaker() as db, db.begin():
        row = (
            await db.execute(
                text(
                    """UPDATE ai_requests SET lease_expires_at = now() + make_interval(secs => :lease),
                              stage = COALESCE(CAST(:stage AS text), stage),
                              state = CASE WHEN CAST(:state AS text) IS NOT NULL AND state = 'running' THEN CAST(:state AS text) ELSE state END
                        WHERE id = :r AND claim_token = :t AND state IN ('running', 'validating', 'cancelling')
                        RETURNING cancel_requested_at, state"""
                ),
                {
                    "lease": settings.ai_request_lease_seconds,
                    "stage": stage,
                    "state": state,
                    "r": request_id,
                    "t": token,
                },
            )
        ).one_or_none()
    if row is None:
        return "lost_claim"
    return "cancelled" if row.cancel_requested_at is not None or row.state == "cancelling" else None


async def finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    request_id: uuid.UUID,
    room_id: uuid.UUID,
    token: uuid.UUID,
    *,
    state: str,
    error_code: str | None = None,
    detail: dict | None = None,
) -> bool:
    """End a claimed request as failed or cancelled. False if the claim was lost."""
    async with sessionmaker() as db, db.begin():
        locked = await lock_room(db, room_id)
        row = (
            await db.execute(
                select(_REQ)
                .where(_REQ.c.id == request_id, _REQ.c.claim_token == token)
                .with_for_update()
            )
        ).one_or_none()
        if locked is None or row is None or row.state not in CLAIM_STATES:
            return False
        if state == "failed":
            await _fail_locked(db, locked, row, error_code or "internal_error", detail, settings)
        else:
            await db.execute(
                update(_REQ)
                .where(_REQ.c.id == row.id)
                .values(
                    state="cancelled",
                    claim_token=None,
                    lease_expires_at=None,
                    finished_at=func.now(),
                    stage=None,
                    retain_until=func.now() + timedelta(days=settings.ai_result_retention_days),
                )
            )
            await _emit(db, locked, row.id, "cancelled", row.requested_by)
    return True


async def requeue(
    sessionmaker: async_sessionmaker[AsyncSession],
    request_id: uuid.UUID,
    room_id: uuid.UUID,
    token: uuid.UUID,
    code: str,
    delay_seconds: int,
) -> bool:
    """A transient failure (the provider was down after the call policy gave up): run again later."""
    async with sessionmaker() as db, db.begin():
        locked = await lock_room(db, room_id)
        row = (
            await db.execute(
                select(_REQ)
                .where(_REQ.c.id == request_id, _REQ.c.claim_token == token)
                .with_for_update()
            )
        ).one_or_none()
        if (
            locked is None
            or row is None
            or row.state not in CLAIM_STATES
            or row.claims >= MAX_CLAIMS
        ):
            return False
        await db.execute(
            update(_REQ)
            .where(_REQ.c.id == row.id)
            .values(
                state="queued",
                claim_token=None,
                lease_expires_at=None,
                stage=None,
                error_code=code,
                run_after=func.now() + timedelta(seconds=delay_seconds),
            )
        )
        await _emit(db, locked, row.id, "queued", row.requested_by)
    return True


async def set_task_type(
    sessionmaker: async_sessionmaker[AsyncSession],
    request_id: uuid.UUID,
    token: uuid.UUID,
    task_type: str,
) -> None:
    async with sessionmaker() as db, db.begin():
        await db.execute(
            update(_REQ)
            .where(_REQ.c.id == request_id, _REQ.c.claim_token == token)
            .values(task_type=task_type)
        )


async def write_manifest(
    sessionmaker: async_sessionmaker[AsyncSession], request_id: uuid.UUID, attempt: int, data: dict
) -> None:
    """Once per attempt (a retry of the same attempt after a crash keeps the first)."""
    async with sessionmaker() as db, db.begin():
        await db.execute(
            pg_insert(_MAN)
            .values(request_id=request_id, attempt=attempt, data=data)
            .on_conflict_do_nothing(index_elements=["request_id", "attempt"])
        )


@dataclass
class Publication:
    task_type: str
    answer: dict[str, Any]
    citations: list[dict[str, Any]]
    coverage: dict[str, Any]
    usage: dict[str, Any]
    models: dict[str, Any]
    body: str
    extras: dict[str, Any] = field(default_factory=dict)
    # phase 2: the proposal row that is born with the result and the chat reply (all or nothing)
    proposal: dict[str, Any] | None = None


async def publish_success(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    row: Any,
    token: uuid.UUID,
    pub: Publication,
) -> str:
    """Write the result and the chat reply in ONE transaction under the room lock. Returns
    `published`, `already_published` (a predecessor got here first) or `lost` (claim lost or
    cancellation requested: nothing is published)."""
    async with sessionmaker() as db, db.begin():
        locked = await lock_room(db, row.room_id)
        current = (
            await db.execute(select(_REQ).where(_REQ.c.id == row.id).with_for_update())
        ).one_or_none()
        if locked is None or current is None:
            return "lost"
        existing = (
            await db.execute(select(_RES.c.id).where(_RES.c.request_id == row.id))
        ).scalar_one_or_none()
        if existing is not None:
            if current.state not in TERMINAL:
                await _mark_succeeded(db, locked, current, settings)
            return "already_published"
        if current.claim_token != token or current.state not in ("running", "validating"):
            return "lost"
        if current.cancel_requested_at is not None:
            return "lost"
        await ensure_system_user(db)
        result_id = (
            await db.execute(
                pg_insert(_RES)
                .values(
                    request_id=row.id,
                    room_id=row.room_id,
                    task_type=pub.task_type,
                    answer=pub.answer,
                    citations=pub.citations,
                    coverage=pub.coverage,
                    usage=pub.usage,
                    models=pub.models,
                )
                .on_conflict_do_nothing(index_elements=["request_id"])
                .returning(_RES.c.id)
            )
        ).scalar_one_or_none()
        if result_id is None:
            return "already_published"
        if pub.proposal is not None:
            proposal_id = (
                await db.execute(
                    pg_insert(_PROP)
                    .values(
                        request_id=row.id,
                        result_id=result_id,
                        room_id=row.room_id,
                        created_by=row.requested_by,
                        state="pending",
                        expires_at=func.now() + timedelta(days=settings.ai_proposal_ttl_days),
                        regenerated_from=row.regenerate_of,
                        **pub.proposal,
                    )
                    .returning(_PROP.c.id)
                )
            ).scalar_one()
            from app.services import (
                ai_proposals,  # late: that module reads requests, not the reverse
            )

            await ai_proposals.emit(db, locked, proposal_id, "pending", row.requested_by)
        reply_id = reply_client_message_id(row.id)
        message_id = await messages.insert_message_row(
            db,
            locked,
            room_id=row.room_id,
            author_id=SYSTEM_USER_ID,
            client_message_id=reply_id,
            body=pub.body,
            fingerprint=hashlib.sha256(b"ai-reply:" + row.id.bytes).digest(),
            kind="ai_answer",
            ai_request_id=row.id,
            actor_id=row.requested_by,
        )
        await db.execute(
            update(_RES).where(_RES.c.id == result_id).values(reply_message_id=message_id)
        )
        await _mark_succeeded(db, locked, current, settings)
    return "published"


async def _mark_succeeded(
    db: AsyncSession, locked: LockedRoom, row: Any, settings: Settings
) -> None:
    await db.execute(
        update(_REQ)
        .where(_REQ.c.id == row.id)
        .values(
            state="succeeded",
            claim_token=None,
            lease_expires_at=None,
            finished_at=func.now(),
            stage=None,
            error_code=None,
            retain_until=func.now() + timedelta(days=settings.ai_result_retention_days),
        )
    )
    await _emit(db, locked, row.id, "succeeded", row.requested_by)


# ---- result reads ----------------------------------------------------------------------------------------------


async def get_result(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, request_id: uuid.UUID
) -> Any:
    async with sessionmaker() as db:
        return (
            await db.execute(
                select(_RES).where(_RES.c.request_id == request_id, _RES.c.room_id == room_id)
            )
        ).one_or_none()


async def proposal_id_of(
    sessionmaker: async_sessionmaker[AsyncSession], request_id: uuid.UUID
) -> uuid.UUID | None:
    async with sessionmaker() as db:
        return (
            await db.execute(select(_PROP.c.id).where(_PROP.c.request_id == request_id))
        ).scalar_one_or_none()


async def get_manifest_summary(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, request_id: uuid.UUID
) -> dict | None:
    """The authorized context summary: WHAT went into the request (files and versions, counts of
    passages and images, the document capture's counter, number of chat messages, models, exclusions),
    never the texts themselves."""
    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(_MAN.c.data, _MAN.c.attempt, _MAN.c.redacted_at)
                .join(_REQ, _REQ.c.id == _MAN.c.request_id)
                .where(_MAN.c.request_id == request_id, _REQ.c.room_id == room_id)
                .order_by(_MAN.c.attempt.desc())
                .limit(1)
            )
        ).one_or_none()
    if row is None:
        return None
    data = row.data or {}
    if row.redacted_at is not None or data.get("redacted"):
        return {"redacted": True}
    return {
        "attempt": row.attempt,
        "task_type": data.get("task_type"),
        "sources": [
            {
                k: s.get(k)
                for k in ("source_version_id", "source_name", "version_no", "generation_id", "mode")
            }
            for s in data.get("sources", [])
        ],
        "passages": len(data.get("passages", [])),
        "images_sent": [
            {k: i.get(k) for k in ("sha256", "page", "figure_number", "label")}
            for i in data.get("images", [])
        ],
        "document": {
            k: (data.get("document") or {}).get(k)
            for k in ("document_id", "yjs_update_counter", "content_hash", "scope", "block_count")
        }
        if data.get("document")
        else None,
        "chat_messages": len(data.get("chat_messages", [])),
        "models": data.get("models", {}),
        "prompt_template": data.get("prompt_template"),
        "exclusions": data.get("exclusions", []),
        "coverage": data.get("coverage", {}),
    }


# ---- source removal: redaction ---------------------------------------------------------------------------------------


async def redact_for_removed_versions(db: AsyncSession, version_ids: list[uuid.UUID]) -> int:
    """Inside source removal's transaction. Published results keep their text but lose the excerpts
    and quotes taken from the removed files (a citation becomes a stub: version id, locator, 'source
    removed'); manifests lose the stored passages of those versions."""
    if not version_ids:
        return 0
    wanted = {str(v) for v in version_ids}
    touched = 0
    requests = (
        (
            await db.execute(
                text(
                    "SELECT DISTINCT request_id FROM ai_request_sources WHERE source_version_id = ANY(:v)"
                ),
                {"v": version_ids},
            )
        )
        .scalars()
        .all()
    )
    for rid in requests:
        res = (
            await db.execute(select(_RES.c.id, _RES.c.citations).where(_RES.c.request_id == rid))
        ).one_or_none()
        if res is not None:
            changed = False
            cites = []
            for c in res.citations or []:
                if c.get("source_version_id") in wanted:
                    c = {k: v for k, v in c.items() if k not in ("excerpt", "quotes")} | {
                        "source_removed": True,
                        "quotes": [],
                    }
                    changed = True
                cites.append(c)
            if changed:
                await db.execute(update(_RES).where(_RES.c.id == res.id).values(citations=cites))
                touched += 1
        for man in (await db.execute(select(_MAN).where(_MAN.c.request_id == rid))).all():
            data = man.data or {}
            if data.get("redacted"):
                continue
            scrubbed = _scrub_manifest(data, wanted)
            if scrubbed != data:
                await db.execute(
                    update(_MAN)
                    .where(_MAN.c.request_id == rid, _MAN.c.attempt == man.attempt)
                    .values(data=scrubbed)
                )
                touched += 1
    return touched


def _scrub_manifest(data: dict, wanted: set[str]) -> dict:
    out = dict(data)
    out["passages"] = [
        {k: v for k, v in p.items() if k not in ("text", "excerpt")} | {"source_removed": True}
        if p.get("source_version_id") in wanted
        else p
        for p in data.get("passages", [])
    ]
    out["images"] = [
        {**i, "source_removed": True} if i.get("source_version_id") in wanted else i
        for i in data.get("images", [])
    ]
    out["sections"] = [
        {k: v for k, v in s.items() if k not in ("text", "summary")} | {"source_removed": True}
        if s.get("source_version_id") in wanted
        else s
        for s in data.get("sections", [])
    ]
    return out


async def figure_of(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, chunk_id: uuid.UUID
) -> Any:
    """The stored image of a figure chunk of THIS room (None when the index or the file is gone)."""
    async with sessionmaker() as db:
        return (
            await db.execute(
                text(
                    "SELECT storage_key, media_type FROM figures "
                    "WHERE chunk_id = :c AND room_id = :r AND storage_key IS NOT NULL"
                ),
                {"c": chunk_id, "r": room_id},
            )
        ).one_or_none()
