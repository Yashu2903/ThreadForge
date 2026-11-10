"""Executing one `@AI` request in the worker, start to finish.

    outcome = await run_request(sessionmaker, settings, storage, row, token)

THE STEPS (the request's `stage`, shown to users as "Gathering context", "Generating", "Validating"):
  0 gate        the SAME admission gate as when the request was accepted, run again by the worker with
                the requester's own rights BEFORE any model or document access (role, can_write on the
                anchor document, switches, consent). The pinned files must still exist.
  1 classify    only for task_hint=auto: one small call with the instruction text alone (no evidence). It
                can choose a processing strategy; it can never widen scope, sources or context.
  2 capture     only if the document was included: the collaboration service's validated state, once;
                the selection check; the document's images.
  3 gather      the strategy (app/ai/answer/strategies.py) assembles labelled evidence from exactly the
                pinned generations and the context the request enabled.
  4 manifest    what is about to be sent is written down, immutably, BEFORE the answer is generated.
  5 generate    one answer call (or the reduce step after map calls), structured output, no tools.
  6 validate    citations, quotes, the 30 % rule with one repair attempt, Mermaid parse with one repair.
  7 publish     re-check access and the files, then ONE transaction: the result and the chat reply.

GUARANTEES
  * no database transaction is open during a provider call or the collaboration call;
  * cancellation is checked at every stage and before every provider call (a late result is discarded,
    its usage stays in the ledger);
  * a crash may repeat the computation (and its spend); it can never publish twice (UNIQUE result,
    deterministic reply message id);
  * a failure publishes nothing, and says why with a stable code.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import access
from app.ai import errors as perr
from app.ai.analysis.gateway import AnalysisAbort, AnalysisCancelled, AnalysisGateway
from app.ai.answer import capture as cap
from app.ai.answer import mermaid, prompts, strategies
from app.ai.answer.answer import (
    ANSWER_SCHEMA,
    ANSWER_SCHEMA_NAME,
    BadAnswer,
    ClaimStats,
    EvidenceSet,
    ValidatedAnswer,
    needs_repair,
    render_plain_text,
    validate_answer,
)
from app.ai.edit import execute as edit_execute
from app.ai.pricing import Prices
from app.ai.types import TextPart, Usage
from app.config import Settings
from app.errors import AppError
from app.logging_config import APP_LOGGER
from app.services import ai_requests as svc
from app.services.memberships import get_active_role
from app.storage import Storage

log = logging.getLogger(f"{APP_LOGGER}.ai.answer")
ANSWER_OUTPUT_TOKENS = 6000
POINTER = "Open the answer card for the full text and the citations."
TRANSIENT = {perr.RATE_LIMITED, perr.UNAVAILABLE, perr.TIMEOUT}


class _Interrupted(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Fail(Exception):
    def __init__(self, code: str, detail: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


async def run_request(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    row: Any,
    token: uuid.UUID,
    *,
    transports: dict[str, httpx2.AsyncBaseTransport] | None = None,
    collab_transport: httpx2.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Run a claimed request to an end. Returns `published`, `already_published`, `cancelled`,
    `lost_claim`, `requeued` or `failed:<code>`."""
    request_id, room_id = row.id, row.room_id
    interrupted: list[str] = []

    async def heartbeat() -> None:
        interval = max(1.0, settings.ai_request_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            reason = await svc.renew(sessionmaker, settings, request_id, token)
            if reason is not None:
                interrupted.append(reason)
                work.cancel()
                return

    work = asyncio.ensure_future(
        _work(sessionmaker, settings, storage, row, token, transports, collab_transport, sleep)
    )
    beat = asyncio.ensure_future(heartbeat())
    outcome = "published"
    try:
        try:
            outcome = await work
        except asyncio.CancelledError:
            if not interrupted:
                raise
            outcome = await _stopped(
                sessionmaker, settings, request_id, room_id, token, interrupted[0]
            )
        except _Interrupted as stop:
            outcome = await _stopped(
                sessionmaker, settings, request_id, room_id, token, stop.reason
            )
        except AnalysisCancelled:
            outcome = await _stopped(
                sessionmaker, settings, request_id, room_id, token, "cancelled"
            )
        except _Fail as failure:
            await svc.finish(
                sessionmaker,
                settings,
                request_id,
                room_id,
                token,
                state="failed",
                error_code=failure.code,
                detail=failure.detail,
            )
            outcome = f"failed:{failure.code}"
        except cap.CaptureFailed as failure:
            if failure.retryable and await svc.requeue(
                sessionmaker, request_id, room_id, token, failure.code, 30
            ):
                outcome = "requeued"
            else:
                await svc.finish(
                    sessionmaker,
                    settings,
                    request_id,
                    room_id,
                    token,
                    state="failed",
                    error_code=failure.code,
                )
                outcome = f"failed:{failure.code}"
        except AnalysisAbort as abort:
            if abort.retryable and await svc.requeue(
                sessionmaker, request_id, room_id, token, abort.code, 30 * max(1, row.claims)
            ):
                outcome = "requeued"
            else:
                await svc.finish(
                    sessionmaker,
                    settings,
                    request_id,
                    room_id,
                    token,
                    state="failed",
                    error_code=abort.code,
                )
                outcome = f"failed:{abort.code}"
        except BadAnswer:
            await svc.finish(
                sessionmaker,
                settings,
                request_id,
                room_id,
                token,
                state="failed",
                error_code="answer_invalid",
            )
            outcome = "failed:answer_invalid"
        except AppError as denied:  # the gate: access, consent, switches, keys
            await svc.finish(
                sessionmaker,
                settings,
                request_id,
                room_id,
                token,
                state="failed",
                error_code=denied.code,
            )
            outcome = f"failed:{denied.code}"
        except Exception:
            log.exception("action=ai_request_crashed request_id=%s", request_id)
            if await svc.requeue(sessionmaker, request_id, room_id, token, "internal_error", 30):
                outcome = "requeued"
            else:
                await svc.finish(
                    sessionmaker,
                    settings,
                    request_id,
                    room_id,
                    token,
                    state="failed",
                    error_code="internal_error",
                )
                outcome = "failed:internal_error"
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await beat
    log.info("action=ai_request_finished request_id=%s outcome=%s", request_id, outcome)
    return outcome


async def _stopped(sessionmaker, settings, request_id, room_id, token, reason: str) -> str:
    if reason == "lost_claim":
        return "lost_claim"
    await svc.finish(sessionmaker, settings, request_id, room_id, token, state="cancelled")
    return "cancelled"


async def _gate(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, row: Any
) -> None:
    role = await get_active_role(sessionmaker, row.room_id, row.requested_by)
    if role is None:
        raise AppError(403, "room_access_lost", "The requester is no longer in this room.")
    async with sessionmaker() as db:
        await access.gate(
            db,
            settings,
            user_id=row.requested_by,
            role=role,
            room_id=row.room_id,
            document_id=row.document_id,
            require_anchor=row.operation != "draft",
        )


def resolve_task(hint: str, classified: str | None, *, n_sources: int, has_doc: bool) -> str:
    """The processing strategy. It follows what the user chose (the hint), or what the classifier said,
    but only among strategies their context supports: it can never add sources or document content."""
    wanted = (
        {"ask": "qa", "summarize": "summarize", "compare": "compare"}.get(hint)
        or classified
        or "qa"
    )
    if wanted == "compare" and n_sources < 2:
        wanted = "qa"
    if wanted == "summarize" and n_sources == 0:
        wanted = "document_review" if has_doc else "qa"
    if wanted == "document_review" and not has_doc:
        wanted = "qa"
    return wanted


async def _work(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    row: Any,
    token: uuid.UUID,
    transports: dict[str, httpx2.AsyncBaseTransport] | None,
    collab_transport: httpx2.AsyncBaseTransport | None,
    sleep: Callable[[float], Awaitable[None]],
) -> str:
    request_id, room_id = row.id, row.room_id

    async def check(stage: str | None = None, state: str | None = None) -> None:
        reason = await svc.renew(
            sessionmaker, settings, request_id, token, stage=stage, state=state
        )
        if reason is not None:
            raise _Interrupted(reason)

    async def is_cancelled() -> bool:
        return await svc.renew(sessionmaker, settings, request_id, token) is not None

    # ---- 0. gate: before any model or document access ------------------------------------------------
    await _gate(sessionmaker, settings, row)
    async with sessionmaker() as db:
        sources = await strategies.load_sources(db, room_id, request_id)
        pinned = (
            await db.execute(
                text("SELECT count(*) FROM ai_request_sources WHERE request_id = :r"),
                {"r": request_id},
            )
        ).scalar_one()
    if len(sources) != pinned:
        raise _Fail("source_removed")

    gateway = AnalysisGateway(
        sessionmaker,
        settings,
        row,
        is_cancelled=is_cancelled,
        transports=transports,
        sleep=sleep,
        mode="request",
    )
    try:
        if row.operation != "answer":
            # phase 2: an edit or draft proposal (same gate, pins, lease and cancellation as an answer)
            try:
                return await edit_execute.run(
                    sessionmaker=sessionmaker,
                    settings=settings,
                    storage=storage,
                    row=row,
                    token=token,
                    gateway=gateway,
                    sources=sources,
                    pinned=pinned,
                    check=check,
                    gate=lambda: _gate(sessionmaker, settings, row),
                    collab_transport=collab_transport,
                )
            except edit_execute.EditFail as failure:
                if failure.retryable:
                    raise AnalysisAbort(failure.code, retryable=True) from None
                raise _Fail(failure.code, failure.detail) from None
        # ---- 1. classify ----------------------------------------------------------------------------------
        await check("gathering_context")
        classified: str | None = None
        if row.task_hint == "auto":
            classified = await _classify(gateway, row.instruction)
        task = resolve_task(
            row.task_hint, classified, n_sources=len(sources), has_doc=row.include_document_content
        )
        await svc.set_task_type(sessionmaker, request_id, token, task)

        # ---- 2. capture ------------------------------------------------------------------------------------
        captured: cap.Captured | None = None
        selection: list[cap.DocBlock] = []
        if row.include_document_content:
            captured = await cap.capture_document(
                settings, row.requested_by, row.document_id, transport=collab_transport
            )
            if row.document_scope == "blocks":
                selection = cap.resolve_selection(
                    captured, list(row.selected_block_ids), row.selection_hash
                )
        await check("gathering_context")

        # ---- 3. gather ------------------------------------------------------------------------------------
        gathered = await _gather(
            sessionmaker, settings, storage, gateway, row, sources, task, captured, selection, check
        )

        # ---- 4. manifest (before generation) ---------------------------------------------------------------
        manifest = _manifest(settings, row, task, sources, gathered, captured, selection)
        await svc.write_manifest(sessionmaker, request_id, row.attempt, manifest)

        # ---- 5-6. generate and validate ----------------------------------------------------------------------
        await check("generating")
        if gathered.empty and not _has_other_context(row, gathered):
            validated = no_evidence_answer()
        else:
            validated = await _answer(gateway, settings, row, task, gathered, check)
        await check(stage="validating", state="validating")

        # ---- 7. publish ----------------------------------------------------------------------------------------
        await _gate(sessionmaker, settings, row)  # access can change while a model thinks
        async with sessionmaker() as db:
            still = await strategies.load_sources(db, room_id, request_id)
        if len(still) != pinned:
            raise _Fail("source_removed")
        pub = _publication(settings, gateway, task, validated, gathered)
        result = await svc.publish_success(sessionmaker, settings, row, token, pub)
        if result == "lost":
            reason = await svc.renew(sessionmaker, settings, request_id, token)
            raise _Interrupted(reason or "lost_claim")
        return result
    finally:
        await gateway.aclose()


def _has_other_context(row: Any, gathered: strategies.Gathered) -> bool:
    return any(k.startswith(("D", "C")) for k in gathered.evidence.items)


async def _classify(gateway: AnalysisGateway, instruction: str) -> str | None:
    try:
        data = await gateway.structured(
            "classify",
            [TextPart(instruction)],
            system=prompts.CLASSIFY_SYSTEM,
            schema=prompts.CLASSIFY_SCHEMA,
            schema_name=prompts.CLASSIFY_SCHEMA_NAME,
            max_output_tokens=50,
        )
        task = data.get("task") if isinstance(data, dict) else None
        return task if task in ("qa", "summarize", "compare", "document_review") else None
    except AnalysisAbort as abort:
        if abort.code in (
            "budget_exhausted",
            "key_rejected",
            "key_unavailable",
            "analysis_cap_exceeded",
        ):
            raise
        return None  # a failed classification means qa, never a failed request


async def _gather(
    sessionmaker, settings, storage, gateway, row, sources, task, captured, selection, check
) -> strategies.Gathered:
    out = strategies.Gathered()
    out.manifest["models"] = {
        "generation": settings.ai_llm_model,
        "embedding": settings.ai_embed_model,
    }
    b = strategies.budgets(
        settings, has_doc=captured is not None, has_chat=row.include_chat_context
    )
    async with sessionmaker() as db:
        # chat messages first (exact copies, explicit ids only)
        chat, excl = await strategies.chat_items(
            db, row.room_id, list(row.chat_message_ids), out.evidence, b["chat"], settings
        )
        out.manifest["chat_messages"] = chat
        out.manifest.setdefault("exclusions", []).extend(excl)

        # the document: selected blocks, or as much of the whole as fits (the rest is sectioned or said)
        doc_info: dict[str, Any] | None = None
        doc_sections_needed = False
        if captured is not None:
            leaves = selection if row.document_scope == "blocks" else captured.leaves()
            fits_all = True
            included = 0
            if task == "document_review" and row.document_scope == "whole":
                total_tokens = sum(strategies.tokens(settings, x.text) for x in leaves)
                if total_tokens > b["document"]:
                    doc_sections_needed = True
            if not doc_sections_needed:
                included, fits_all = strategies.doc_items(
                    captured, leaves, out.evidence, b["document"], settings
                )
            doc_info = {
                "document_id": str(captured.document_id),
                "yjs_update_counter": captured.counter,
                "content_hash": captured.content_hash,
                "scope": row.document_scope,
                "block_count": len(leaves),
                "blocks": [
                    {"id": x.id, "hash": x.hash, "type": x.type, "text": x.text[:2000]}
                    for x in leaves
                ],
            }
            out.coverage["document"] = {
                "document_id": str(captured.document_id),
                "captured_update_counter": captured.counter,
                "scope": row.document_scope,
                "blocks_total": len(leaves),
                "blocks_included": included if not doc_sections_needed else len(leaves),
                "mode": "sectioned"
                if doc_sections_needed
                else ("full" if fits_all else "truncated"),
            }
            if not fits_all and not doc_sections_needed:
                out.manifest["exclusions"].append("document_truncated_to_budget")
        out.manifest["document"] = doc_info
        out.coverage["chat_messages_included"] = len(chat)

        if task == "summarize" and sources:
            await strategies.gather_summary(
                db,
                gateway,
                settings,
                row.room_id,
                sources,
                out,
                max_calls=max(1, settings.ai_max_provider_calls - 6),
            )
            out.note = "Cite the S labels. Each S section is a summary of one part of a file."
        elif task == "document_review" and doc_sections_needed and captured is not None:
            secs = strategies.document_sections(captured)
            recs, mode = await strategies.summarize_sections(
                gateway,
                settings,
                secs,
                out.evidence,
                max_calls=max(1, settings.ai_max_provider_calls - 6),
                doc_mode=True,
            )
            out.manifest.setdefault("sections", []).extend(recs)
            out.coverage["document"]["mode"] = "sectioned" if mode == "full" else "sampled"
            out.note = "The document is long: each S section is a summary of one heading's text."
            if sources:
                await strategies.gather_retrieval(
                    db,
                    gateway,
                    settings,
                    storage,
                    row.room_id,
                    sources,
                    row.instruction,
                    out,
                    b["evidence"] // 2,
                    balanced=False,
                )
        elif sources:
            await strategies.gather_retrieval(
                db,
                gateway,
                settings,
                storage,
                row.room_id,
                sources,
                row.instruction,
                out,
                b["evidence"],
                balanced=(task == "compare"),
            )
        await check("gathering_context")
        # images: figure images chosen by retrieval come first; document images fill what is left
        if captured is not None:
            room_left = max(0, settings.ai_max_images - len(out.images))
            out.images += await _document_images(
                db,
                storage,
                row,
                captured,
                selection if row.document_scope == "blocks" else None,
                room_left,
            )
    if not sources:
        out.empty = False
    out.coverage["task"] = task
    out.manifest["task_type"] = task
    out.manifest["coverage"] = out.coverage
    return out


async def _document_images(
    db: AsyncSession, storage: Storage, row: Any, captured: cap.Captured, selection, room_left: int
) -> list[strategies.ImageToSend]:
    if room_left <= 0:
        return []
    ids = cap.asset_ids_in(captured.content, {b.id for b in selection} if selection else None)
    if not ids:
        return []
    assets = (
        await db.execute(
            text(
                """SELECT id, storage_key, media_type FROM room_assets
                    WHERE room_id = :room AND id = ANY(CAST(:ids AS uuid[])) AND state = 'attached'"""
            ),
            {"room": row.room_id, "ids": ids},
        )
    ).all()
    out: list[strategies.ImageToSend] = []
    for a in assets[:room_left]:
        try:
            data = storage.path_for("assets", a.storage_key).read_bytes()
        except (OSError, ValueError):
            continue
        out.append(
            strategies.ImageToSend(
                label=f"image from the open document ({len(out) + 1})",
                data=data,
                media_type=a.media_type,
                sha256=hashlib.sha256(data).hexdigest(),
                page=None,
                figure_number=None,
                source_version_id=None,
                chunk_id=None,
            )
        )
    return out


def _manifest(
    settings, row, task, sources, g: strategies.Gathered, captured, selection
) -> dict[str, Any]:
    m = dict(g.manifest)
    m.update(
        {
            "task_type": task,
            "prompt_template": prompts.PROMPT_TEMPLATE_VERSION,
            "attempt": row.attempt,
            "instruction_sha256": hashlib.sha256(row.instruction.encode()).hexdigest(),
            "sources": [
                {
                    "source_version_id": str(s.version_id),
                    "source_name": s.name,
                    "version_no": s.version_no,
                    "kind": s.kind,
                    "generation_id": str(s.generation_id),
                }
                for s in sources
            ],
            "images": [
                {
                    "label": i.label,
                    "sha256": i.sha256,
                    "page": i.page,
                    "figure_number": i.figure_number,
                    "source_version_id": str(i.source_version_id) if i.source_version_id else None,
                    "chunk_id": str(i.chunk_id) if i.chunk_id else None,
                    "media_type": i.media_type,
                }
                for i in g.images
            ],
            "config": {
                "max_input_tokens": settings.ai_max_input_tokens,
                "max_output_tokens": settings.ai_max_output_tokens,
                "evidence_tokens": settings.ai_retrieval_evidence_tokens,
                "rrf_k": settings.ai_retrieval_rrf_k,
                "chunk_policy": "chunk-v1",
                "key_sources": {
                    "generation": row.key_source_generation,
                    "embedding": row.key_source_embedding,
                },
            },
        }
    )
    m.setdefault("passages", [])
    m.setdefault("sections", [])
    m.setdefault("chat_messages", [])
    m.setdefault("exclusions", [])
    return m


def no_evidence_answer() -> ValidatedAnswer:
    """Nothing relevant in the selected files and no other context: say so, without asking the model
    (it could only invent something)."""
    return ValidatedAnswer(
        blocks=[
            {
                "type": "paragraph",
                "text": "I could not find anything relevant to this request in the selected files.",
                "citations": [],
                "unsupported": False,
                "note": "no_evidence",
            }
        ],
        citations=[],
        evidence_gaps=["Nothing in the selected files matched the request."],
        stats=ClaimStats(),
        diagrams=[],
    )


async def _answer(gateway, settings, row, task, g: strategies.Gathered, check) -> ValidatedAnswer:
    """The answer call, then validation with at most one repair for shape/support and one for diagrams."""
    images = [(i.label, i.data, i.media_type) for i in g.images]
    base = prompts.user_parts(row.instruction, g.evidence, images=images, note=g.note)
    system = prompts.system_prompt(task)
    max_out = min(ANSWER_OUTPUT_TOKENS, settings.ai_max_output_tokens)

    async def call(extra: str | None = None) -> Any:
        parts = list(base) + ([TextPart(prompts.repair_note(extra))] if extra else [])
        try:
            return await gateway.structured(
                "answer",
                parts,
                system=system,
                schema=ANSWER_SCHEMA,
                schema_name=ANSWER_SCHEMA_NAME,
                max_output_tokens=max_out,
            )
        except AnalysisAbort as abort:
            if (
                abort.code == perr.BAD_OUTPUT
            ):  # not valid JSON: the same one repair as a wrong shape
                return None
            raise

    problem: str | None = None
    validated: ValidatedAnswer | None = None
    try:
        validated = validate_answer(await call(), g.evidence)
        if needs_repair(validated.stats):
            problem = _support_problem(validated.stats)
    except BadAnswer as bad:
        problem = f"the reply did not match the required shape ({bad})"
    if problem is not None:
        await check("generating")
        try:
            validated = validate_answer(await call(problem), g.evidence)
        except BadAnswer:
            raise
        if needs_repair(validated.stats):
            raise _Fail(
                "answer_unsupported",
                {
                    "unsupported_claims": validated.stats.unsupported,
                    "claims": validated.stats.claims,
                },
            )
    assert validated is not None  # noqa: S101 - set above or an exception left the function

    if validated.diagrams:
        validated = await _check_diagrams(settings, validated, call, g.evidence, check)
    return validated


def _support_problem(stats: ClaimStats) -> str:
    return (
        f"{stats.unsupported} of {stats.claims} claims had no valid citation "
        f"({stats.invalid_citations} citations named a label that does not exist or quoted text that is "
        "not in the evidence). Every claim must cite existing labels, and quotes must be exact."
    )


async def _check_diagrams(
    settings, validated: ValidatedAnswer, call, evidence: EvidenceSet, check
) -> ValidatedAnswer:
    sources = [validated.blocks[i]["mermaid"] for i in validated.diagrams]
    results = await mermaid.parse_check(settings, sources)
    failed = [(n + 1, r) for n, r in enumerate(results) if r is not None and not r.ok]
    if failed:
        await check("generating")
        problem = "; ".join(f"diagram {n} is invalid Mermaid: {r.message}" for n, r in failed)
        try:
            retry = validate_answer(
                await call(problem + ". Fix only the diagram(s), keep the rest."), evidence
            )
            if not needs_repair(retry.stats) and retry.diagrams:
                validated = retry
                sources = [validated.blocks[i]["mermaid"] for i in validated.diagrams]
                results = await mermaid.parse_check(settings, sources)
        except BadAnswer:
            pass  # keep the first answer; its diagram is shown with the error
    for i, result in zip(validated.diagrams, results, strict=True):
        block = validated.blocks[i]
        if result is None:
            block["render_status"], block["render_error"] = "unchecked", None
        elif result.ok:
            block["render_status"], block["render_error"] = "ok", None
        else:
            block["render_status"], block["render_error"] = "failed", result.message
    return validated


def _publication(
    settings, gateway: AnalysisGateway, task, v: ValidatedAnswer, g: strategies.Gathered
):
    t = gateway.totals
    prices = Prices.of(settings)
    cost = prices.llm_cost(Usage(t["llm_in"], t["llm_out"])) + prices.embed_text_cost(
        Usage(t["embed_tokens"])
    )
    return svc.Publication(
        task_type=task,
        answer={
            "blocks": v.blocks,
            "evidence_gaps": v.evidence_gaps,
            "stats": {
                "claims": v.stats.claims,
                "unsupported": v.stats.unsupported,
                "invalid_citations": v.stats.invalid_citations,
                "dropped_quotes": v.stats.dropped_quotes,
            },
        },
        citations=v.citations,
        coverage=g.coverage,
        usage={**t, "est_cost_micros": cost},
        models={
            "generation": settings.ai_llm_model,
            "embedding": settings.ai_embed_model,
            "prompt": prompts.PROMPT_TEMPLATE_VERSION,
        },
        body=render_plain_text(v, 4000, POINTER),
    )


async def process_next_request(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    *,
    transports: dict[str, httpx2.AsyncBaseTransport] | None = None,
    collab_transport: httpx2.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str | None:
    """Claim one queued request (if any) and run it. None when nothing is queued."""
    claimed = await svc.claim_next(sessionmaker, settings)
    if claimed is None:
        return None
    row, token = claimed
    return await run_request(
        sessionmaker,
        settings,
        storage,
        row,
        token,
        transports=transports,
        collab_transport=collab_transport,
        sleep=sleep,
    )
