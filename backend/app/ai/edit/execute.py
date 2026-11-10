"""Executing an edit or draft request in the worker (the phase 2 counterpart of answer/execute.py `_work`).

The admission gate, the pinned files, the lease and the cancellation checks are the SAME as for answers: the
caller (`app.ai.answer.execute._work`) has already run the gate and built the metered gateway, and hands them
in. What is different here:

  EDIT   capture the target document ONCE (the collaboration service's validated state, for the REQUESTER),
         resolve the target the person chose (blocks / ranges / insertion point; `selection_changed` or
         `target_required` otherwise), show the model only that scope, a table of contents and the evidence of
         the selected files, and ask for typed operations;
  DRAFT  no document: evidence only, the model returns a title and blocks;
  both   validate (shape, labels, citations, the candidate document with the shared engine, diagrams), repair
         once, and publish a PENDING proposal together with the chat reply in one transaction. A completed job
         NEVER touches the document, and never changes the room's active document.

The model sees: the instruction, labels like B12, text. It never sees an id, a hash, HTML or Yjs bytes.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.ai.answer import capture as cap
from app.ai.answer import strategies
from app.ai.edit import prompts
from app.ai.edit.check import CheckUnavailable
from app.ai.edit.generate import Generated, ProposalInvalid, generate_draft, generate_edit
from app.ai.edit.target import EditTarget, linearize, resolve_edit_target
from app.ai.pricing import Prices
from app.ai.types import Usage
from app.logging_config import APP_LOGGER
from app.services import ai_requests as svc

log = logging.getLogger(f"{APP_LOGGER}.ai.proposals")
POINTER = "Open the review card to preview the change."


class EditFail(Exception):
    """A request-level failure with a stable code (the caller turns it into a failed request)."""

    def __init__(self, code: str, detail: dict | None = None, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


async def run(
    *,
    sessionmaker,
    settings,
    storage,
    row: Any,
    token,
    gateway,
    sources: list[strategies.SourceInfo],
    pinned: int,
    check,
    gate,
    collab_transport,
) -> str:
    request_id, room_id = row.id, row.room_id
    edit = row.operation == "edit"
    await svc.set_task_type(sessionmaker, request_id, token, "edit_proposal" if edit else "draft")

    # ---- capture and target (edit only) ------------------------------------------------------------------
    captured: cap.Captured | None = None
    target: EditTarget | None = None
    view = None
    title: str | None = None
    if edit:
        captured = await cap.capture_document(
            settings, row.requested_by, row.document_id, transport=collab_transport
        )
        target = resolve_edit_target(
            captured,
            block_ids=[str(i) for i in row.selected_block_ids],
            selection_hash=row.selection_hash,
            ranges=list(row.selected_ranges),
            insert_at=row.insert_at,
        )
        view = linearize(captured, target)
        async with sessionmaker() as db:
            title = (
                await db.execute(
                    text("SELECT title FROM documents WHERE id = :d AND room_id = :r"),
                    {"d": row.document_id, "r": room_id},
                )
            ).scalar_one()
    await check("gathering_context")

    # ---- gather the evidence of the selected files (and explicit chat context) --------------------------
    out = strategies.Gathered()
    out.manifest["models"] = {
        "generation": settings.ai_llm_model,
        "embedding": settings.ai_embed_model,
    }
    budgets = strategies.budgets(settings, has_doc=False, has_chat=row.include_chat_context)
    async with sessionmaker() as db:
        chat, excl = await strategies.chat_items(
            db, room_id, list(row.chat_message_ids), out.evidence, budgets["chat"], settings
        )
        out.manifest["chat_messages"] = chat
        out.manifest.setdefault("exclusions", []).extend(excl)
        if sources:
            await strategies.gather_retrieval(
                db,
                gateway,
                settings,
                storage,
                room_id,
                sources,
                row.instruction,
                out,
                budgets["evidence"],
                balanced=False,
            )
    await check("gathering_context")
    out.coverage["chat_messages_included"] = len(chat)
    if sources and not any(k.startswith(("P", "S")) for k in out.evidence.items) and not edit:
        # a draft with nothing to draw on would be invented text: say so instead
        raise EditFail("no_evidence", {"sources": len(sources)})
    task = "edit_proposal" if edit else "draft"
    out.coverage["task"] = task
    if edit and captured is not None and target is not None:
        out.coverage["document"] = {
            "document_id": str(captured.document_id),
            "captured_update_counter": captured.counter,
            "scope": "edit",
            "blocks_total": len(captured.blocks),
            "blocks_included": len(target.scope_ids),
            "mode": "target_only",
        }
        out.manifest["document"] = {
            "document_id": str(captured.document_id),
            "yjs_update_counter": captured.counter,
            "content_hash": captured.content_hash,
            "scope": target.scope_json(),
            "base_hashes": target.base_hashes,
        }
    out.coverage["sources"] = len(sources)
    out.manifest["task_type"] = task

    # ---- manifest (before generation) ---------------------------------------------------------------------
    from app.ai.answer.execute import _manifest  # lazy: that module imports this one

    manifest = _manifest(settings, row, task, sources, out, captured, [])
    manifest["prompt_template"] = prompts.PROMPT_TEMPLATE_VERSION
    manifest["operation"] = row.operation
    await svc.write_manifest(sessionmaker, request_id, row.attempt, manifest)

    # ---- generate and validate ------------------------------------------------------------------------------
    await check("generating")
    images = [(i.label, i.data, i.media_type) for i in out.images]
    try:
        if edit:
            assert captured is not None and target is not None and view is not None  # noqa: S101
            generated = await generate_edit(
                gateway,
                settings,
                instruction=row.instruction,
                captured_content=captured.content,
                target=target,
                view=view,
                evidence=out.evidence,
                images=images,
                note=out.note,
                check=lambda: check("generating"),
            )
        else:
            generated = await generate_draft(
                gateway,
                settings,
                instruction=row.instruction,
                evidence=out.evidence,
                images=images,
                note=out.note,
                check=lambda: check("generating"),
            )
    except ProposalInvalid as bad:
        raise EditFail(
            "proposal_invalid",
            {"problems": bad.errors[:5], "metrics": bad.metrics},
        ) from None
    except CheckUnavailable as unavailable:
        raise EditFail(
            "proposal_check_unavailable", {"why": str(unavailable)}, retryable=True
        ) from None
    await check(stage="validating", state="validating")

    # ---- publish (access is re-checked: it can change while a model thinks) ----------------------------------
    await gate()
    async with sessionmaker() as db:
        still = await strategies.load_sources(db, room_id, request_id)
    if len(still) != pinned:
        raise EditFail("source_removed")
    pub = _publication(settings, gateway, row, task, generated, out, target, title)
    result = await svc.publish_success(sessionmaker, settings, row, token, pub)
    return result


def _publication(
    settings,
    gateway,
    row: Any,
    task: str,
    g: Generated,
    out: strategies.Gathered,
    target: EditTarget | None,
    document_title: str | None,
) -> svc.Publication:
    totals = gateway.totals
    prices = Prices.of(settings)
    cost = prices.llm_cost(Usage(totals["llm_in"], totals["llm_out"])) + prices.embed_text_cost(
        Usage(totals["embed_tokens"])
    )
    count = len(g.operations) if g.kind == "edit" else len(g.operations["blocks"])
    if g.kind == "edit":
        body = (
            f"Proposed {count} change{'s' if count != 1 else ''} to “{document_title}”"
            f"{': ' + g.summary if g.summary else '.'}\n{POINTER}"
        )
    else:
        body = f"Drafted “{g.title}”{': ' + g.summary if g.summary else '.'}\n{POINTER}"
    coverage = dict(out.coverage)
    coverage["validation"] = g.metrics
    if target is not None:
        coverage["target"] = {
            "blocks": len(target.block_ids),
            "ranges": len(target.ranges),
            "insert_at": target.insert_at,
        }
    answer = {
        "kind": g.kind,
        "summary": g.summary,
        "title": g.title,
        "operation_count": count,
        "evidence_gaps": g.gaps,
        "stats": {
            "claims": g.stats.claims,
            "unsupported": g.stats.unsupported,
            "invalid_citations": g.stats.invalid_citations,
            "dropped_quotes": g.stats.dropped_quotes,
        },
    }
    proposal = {
        "kind": g.kind,
        "document_id": row.document_id if g.kind == "edit" else None,
        "operations": g.operations
        if g.kind == "edit"
        else {
            "title": g.title,
            "blocks": g.operations["blocks"],
            "include_sources_section": g.operations["include_sources_section"],
        },
        "before_content": target.before_json() if target else {},
        "base_hashes": target.base_hashes if target else {},
        "scope": target.scope_json() if target else {},
        "preview": preview_of(g),
        "captured_counter": target.counter if target else None,
        "citations": g.table,
        "document_title": document_title if g.kind == "edit" else g.title,
    }
    return svc.Publication(
        task_type=task,
        answer=answer,
        citations=g.table,
        coverage=coverage,
        usage={**totals, "est_cost_micros": cost},
        models={
            "generation": settings.ai_llm_model,
            "embedding": settings.ai_embed_model,
            "prompt": prompts.PROMPT_TEMPLATE_VERSION,
        },
        body=body[:4000],
        proposal=proposal,
    )


def preview_of(g: Generated) -> dict[str, Any]:
    """What the review card shows (stored with the proposal, so previewing never re-runs anything and never
    touches the live document). Edit: each change with the block(s) before and after, the model's reason and
    its citation numbers. Draft: the validated document itself."""
    if g.kind == "draft":
        return {
            "kind": "draft",
            "title": g.title,
            "summary": g.summary,
            "document": g.doc,
            "block_citations": [m.get("citations", []) for m in g.meta],
            "unsupported_blocks": sum(1 for m in g.meta if m.get("unsupported")),
        }
    changes = []
    for change in g.changes:
        meta = g.meta[change["op"]] if change["op"] < len(g.meta) else {}
        changes.append(
            {
                "op": change["op"],
                "kind": change["kind"],
                "block_id": change.get("block_id"),
                "anchor": change.get("anchor"),
                "before": change["before"],
                "after": change["after"],
                "reason": meta.get("reason", ""),
                "citations": meta.get("citations", []),
                "unsupported": bool(meta.get("unsupported")),
                "hash_after": [
                    g.hashes.get(str((n.get("attrs") or {}).get("id"))) for n in change["after"]
                ],
            }
        )
    return {
        "kind": "edit",
        "summary": g.summary,
        "changes": changes,
        "inserted_chars": g.inserted_chars,
    }
