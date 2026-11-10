"""Generating a proposal: one model call, validation, and at most ONE repair.

    generated = await generate_edit(...)     # or generate_draft(...)

THE LOOP (the same discipline as phase 1 answers):
  call -> normalize (shape, labels, citations) -> build and validate the CANDIDATE with the shared
  TypeScript engine -> diagrams parse -> done.
  If anything fails, the problems are fed back ONCE ("your previous reply was rejected because ...") and the
  model is asked again. If the second reply fails too, the request fails visibly (`proposal_invalid`).
  A partially valid proposal is never published.

THE NUMBERS THE EVALUATION GATE NEEDS are recorded as `metrics` on every result: whether the FIRST reply was
valid, whether the repair was used and fixed it, and the codes of the first reply's problems.
Stub or real, the harness reads them from here (tests/eval/proposals.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai import errors as perr
from app.ai.analysis.gateway import AnalysisAbort, AnalysisGateway
from app.ai.answer import mermaid
from app.ai.answer.answer import ClaimStats, EvidenceSet, needs_repair
from app.ai.edit import prompts
from app.ai.edit.check import CheckOutcome, run_check
from app.ai.edit.schema import (
    DRAFT_SCHEMA,
    DRAFT_SCHEMA_NAME,
    EDIT_SCHEMA,
    EDIT_SCHEMA_NAME,
    BadProposal,
    NormalizedDraft,
    NormalizedEdit,
    describe_problems,
    normalize_draft,
    normalize_edit,
    problem,
    sources_section,
)
from app.ai.edit.target import EditTarget, Linearized
from app.ai.types import Part, TextPart
from app.config import Settings

PROPOSAL_OUTPUT_TOKENS = 8000


class ProposalInvalid(Exception):
    """Both the first reply and the repaired one failed validation."""

    def __init__(self, errors: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
        super().__init__("proposal_invalid")
        self.errors = errors
        self.metrics = metrics


@dataclass
class Generated:
    kind: str  # "edit" | "draft"
    summary: str
    title: str | None
    operations: (
        Any  # edit: the typed operations; draft: {"blocks": [...], "include_sources_section": bool}
    )
    meta: list[dict[str, Any]]
    table: list[dict[str, Any]]
    gaps: list[str]
    stats: ClaimStats
    changes: list[dict[str, Any]] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)
    doc: dict[str, Any] | None = None
    inserted_chars: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)


def _codes(errors: list[dict[str, Any]]) -> list[str]:
    return sorted({str(e.get("code", "invalid")) for e in errors})


async def _diagram_problems(
    settings: Settings, diagrams: list[tuple[int, str]], *, draft: bool
) -> list[dict[str, Any]]:
    """Size ("split it") and the Mermaid parse of every diagram the proposal writes."""
    errors: list[dict[str, Any]] = []
    for index, source in diagrams:
        if len(source) > settings.ai_diagram_split_chars:
            errors.append(
                problem(
                    None if draft else index,
                    "diagram_too_large",
                    f"a diagram of {len(source)} characters is too large to read; split it into "
                    f"smaller diagrams of at most {settings.ai_diagram_split_chars} characters",
                )
            )
    if errors or not diagrams:
        return errors
    results = await mermaid.parse_check(settings, [s for _, s in diagrams])
    for (index, _), result in zip(diagrams, results, strict=True):
        if result is not None and not result.ok:
            errors.append(
                problem(
                    None if draft else index,
                    "diagram_invalid",
                    f"the diagram is not valid Mermaid: {result.message}",
                )
            )
    return errors


async def _call(
    gateway: AnalysisGateway,
    base: list[Part],
    extra: str | None,
    *,
    system: str,
    schema: dict[str, Any],
    schema_name: str,
    max_out: int,
) -> Any:
    parts = list(base) + ([TextPart(prompts.repair_note(extra))] if extra else [])
    try:
        return await gateway.structured(
            "answer",
            parts,
            system=system,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_out,
        )
    except AnalysisAbort as abort:
        if abort.code == perr.BAD_OUTPUT:  # not valid JSON: the same one repair as any other defect
            return None
        raise


async def _generate(call, validate, check) -> tuple[Any, dict[str, Any]]:
    """The first reply, validation, at most one repair. Returns (validated, metrics)."""
    raw = await call(None)
    first = await validate(raw)
    metrics: dict[str, Any] = {"first_try_valid": first[1] == [], "repaired": False}
    if not first[1]:
        return first[0], metrics
    metrics["first_problems"] = _codes(first[1])
    await check()
    raw = await call(describe_problems(first[1]))
    second = await validate(raw)
    metrics["repaired"] = True
    if second[1]:
        metrics["final_problems"] = _codes(second[1])
        raise ProposalInvalid(second[1], metrics)
    return second[0], metrics


async def generate_edit(
    gateway: AnalysisGateway,
    settings: Settings,
    *,
    instruction: str,
    captured_content: dict[str, Any],
    target: EditTarget,
    view: Linearized,
    evidence: EvidenceSet,
    images: list[tuple[str, bytes, str]],
    note: str,
    check,
) -> Generated:
    base = prompts.edit_user_parts(instruction, evidence, view, target, images=images, note=note)
    has_evidence = any(not lb.startswith("C") for lb in evidence.items)
    scope = {
        "whole_ids": sorted(target.whole_ids),
        "ranges": [{"block_id": r.block_id, "start": r.start, "end": r.end} for r in target.ranges],
        "anchors": target.anchors,
    }

    async def validate(raw: Any) -> tuple[tuple[NormalizedEdit, CheckOutcome] | None, list[dict]]:
        if raw is None:
            return None, [problem(None, "not_json", "the reply was not valid JSON for the schema")]
        try:
            edit = normalize_edit(raw, view, evidence)
        except BadProposal as bad:
            return None, bad.errors
        errors: list[dict[str, Any]] = []
        if has_evidence and needs_repair(edit.stats):
            errors.append(
                problem(
                    None,
                    "unsupported",
                    f"{edit.stats.unsupported} of {edit.stats.claims} operations cited no existing "
                    "evidence label (or quoted text that is not in it); every operation that adds or "
                    "changes content must cite existing labels",
                )
            )
        outcome = await run_check(
            settings,
            {
                "mode": "edit",
                "content": captured_content,
                "operations": edit.operations,
                "scope": scope,
            },
        )
        if not outcome.ok:
            errors.extend(outcome.errors)
        elif not errors:
            errors.extend(await _diagram_problems(settings, edit.diagrams, draft=False))
        return ((edit, outcome) if not errors else None), errors

    async def call(extra: str | None) -> Any:
        return await _call(
            gateway,
            base,
            extra,
            system=prompts.edit_system(),
            schema=EDIT_SCHEMA,
            schema_name=EDIT_SCHEMA_NAME,
            max_out=min(PROPOSAL_OUTPUT_TOKENS, settings.ai_max_output_tokens),
        )

    (edit, outcome), metrics = await _generate(call, validate, check)
    return Generated(
        kind="edit",
        summary=edit.summary,
        title=None,
        operations=edit.operations,
        meta=edit.meta,
        table=edit.table,
        gaps=edit.gaps,
        stats=edit.stats,
        changes=outcome.changes,
        hashes=outcome.hashes,
        inserted_chars=outcome.inserted_chars,
        metrics=metrics,
    )


async def generate_draft(
    gateway: AnalysisGateway,
    settings: Settings,
    *,
    instruction: str,
    evidence: EvidenceSet,
    images: list[tuple[str, bytes, str]],
    note: str,
    check,
) -> Generated:
    base = prompts.draft_user_parts(instruction, evidence, images=images, note=note)

    async def validate(
        raw: Any,
    ) -> tuple[tuple[NormalizedDraft, CheckOutcome, list] | None, list[dict]]:
        if raw is None:
            return None, [problem(None, "not_json", "the reply was not valid JSON for the schema")]
        try:
            draft = normalize_draft(raw, evidence)
        except BadProposal as bad:
            return None, bad.errors
        errors: list[dict[str, Any]] = []
        if needs_repair(draft.stats):
            errors.append(
                problem(
                    None,
                    "unsupported",
                    f"{draft.stats.unsupported} of {draft.stats.claims} blocks cited no existing "
                    "evidence label (or quoted text that is not in it); every block with content must "
                    "cite existing labels",
                )
            )
        blocks = list(draft.blocks)
        if draft.include_sources_section:
            blocks += sources_section(draft.table)
        outcome = await run_check(settings, {"mode": "draft", "blocks": blocks})
        if not outcome.ok:
            errors.extend(outcome.errors)
        elif not errors:
            errors.extend(await _diagram_problems(settings, draft.diagrams, draft=True))
        return ((draft, outcome, blocks) if not errors else None), errors

    async def call(extra: str | None) -> Any:
        return await _call(
            gateway,
            base,
            extra,
            system=prompts.draft_system(),
            schema=DRAFT_SCHEMA,
            schema_name=DRAFT_SCHEMA_NAME,
            max_out=min(PROPOSAL_OUTPUT_TOKENS, settings.ai_max_output_tokens),
        )

    (draft, outcome, blocks), metrics = await _generate(call, validate, check)
    return Generated(
        kind="draft",
        summary=draft.summary,
        title=draft.title,
        operations={"blocks": blocks, "include_sources_section": draft.include_sources_section},
        meta=draft.meta,
        table=draft.table,
        gaps=draft.gaps,
        stats=draft.stats,
        doc=outcome.doc,
        inserted_chars=outcome.inserted_chars,
        metrics=metrics,
    )
