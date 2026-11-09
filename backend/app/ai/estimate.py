"""Request estimates: approximate tokens, calls and cost BEFORE anything is sent.

What is estimated now (checkpoint 1): the instruction, the document scope, selected text, chat
messages and the fixed prompt overhead. Sources (checkpoints 2-3) add their evidence tokens and the
analysis they would trigger when those exist.

Two numbers per estimate:
  * expected   what the answer will probably cost  (shown to the user)
  * worst case the reservation made against server budgets: input as estimated + max output tokens
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.pricing import Prices, estimate_text_tokens
from app.ai.types import Usage
from app.config import Settings
from app.models import Document, DocumentSnapshot
from app.services import messages

PROMPT_OVERHEAD_TOKENS = 800  # the fixed instructions, answer schema and citation rules
EXPECTED_OUTPUT_TOKENS = 1500
MAX_CHAT_CONTEXT = 20

_DOCS = Document.__table__
_SNAPS = DocumentSnapshot.__table__


@dataclass
class Estimate:
    input_tokens: int
    expected_output_tokens: int
    max_output_tokens: int
    calls: int
    images: int
    cost_known: bool
    expected_cost_micros: int | None
    max_cost_micros: int | None
    requires_confirmation: bool
    over_caps: list[str] = field(default_factory=list)


async def document_chars(db: AsyncSession, document_id: uuid.UUID) -> int:
    """Characters of document text: the newest validated snapshot's text_length when there is one,
    else the size of the legacy JSON (an over-estimate: it includes JSON markup, which is safe)."""
    snap = (
        await db.execute(
            select(_SNAPS.c.text_length)
            .where(_SNAPS.c.document_id == document_id)
            .order_by(_SNAPS.c.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if snap is not None:
        return int(snap)
    legacy = (
        await db.execute(
            select(func.length(cast(_DOCS.c.content, Text))).where(_DOCS.c.id == document_id)
        )
    ).scalar_one_or_none()
    return int(legacy or 0)


def needs_confirmation(settings: Settings, est: Estimate) -> bool:
    """Above AI_CONFIRM_THRESHOLD the send route requires confirm_large=true (409
    confirmation_required otherwise). The threshold is in micro-dollars when prices are known,
    else in tokens."""
    if est.cost_known and settings.ai_confirm_threshold_micros is not None:
        return (est.expected_cost_micros or 0) > settings.ai_confirm_threshold_micros
    return est.input_tokens + est.expected_output_tokens > settings.ai_confirm_threshold_tokens


async def estimate_request(
    db: AsyncSession,
    settings: Settings,
    *,
    room_id: uuid.UUID,
    document_id: uuid.UUID | None,
    instruction: str,
    task_hint: str,
    document_scope: str,
    include_document_content: bool,
    selected_chars: int,
    chat_message_ids: list[uuid.UUID],
) -> Estimate:
    chars = len(instruction)
    if include_document_content and document_id is not None:
        if document_scope == "whole":
            chars += await document_chars(db, document_id)
        elif document_scope == "blocks":
            chars += selected_chars
    chars += await messages.chat_chars(db, room_id, chat_message_ids[:MAX_CHAT_CONTEXT])
    input_tokens = PROMPT_OVERHEAD_TOKENS + estimate_text_tokens(settings, "x" * chars)
    expected_out = min(EXPECTED_OUTPUT_TOKENS, settings.ai_max_output_tokens)
    calls = 1 + (1 if task_hint == "auto" else 0)  # classification is one small extra call

    prices = Prices.of(settings)
    expected_cost = max_cost = None
    if prices.llm_known:
        expected_cost = prices.llm_cost(Usage(input_tokens, expected_out))
        max_cost = prices.llm_cost(Usage(input_tokens, settings.ai_max_output_tokens))
        if task_hint == "auto":  # the classification call: instruction only
            small = Usage(
                PROMPT_OVERHEAD_TOKENS // 4 + estimate_text_tokens(settings, instruction), 50
            )
            expected_cost += prices.llm_cost(small)
            max_cost += prices.llm_cost(small)

    over: list[str] = []
    if input_tokens > settings.ai_max_input_tokens:
        over.append("max_input_tokens")
    if calls > settings.ai_max_provider_calls:
        over.append("max_provider_calls")
    est = Estimate(
        input_tokens=input_tokens,
        expected_output_tokens=expected_out,
        max_output_tokens=settings.ai_max_output_tokens,
        calls=calls,
        images=0,
        cost_known=prices.llm_known,
        expected_cost_micros=expected_cost,
        max_cost_micros=max_cost,
        requires_confirmation=False,
        over_caps=over,
    )
    est.requires_confirmation = needs_confirmation(settings, est)
    return est


def round_up_thousands(value: int) -> int:
    """UI-friendly rounding for 'about N tokens'."""
    return int(math.ceil(value / 100) * 100)


# ---- analysis of a source version (checkpoint 3) ---------------------------------------------------


@dataclass
class AnalysisEstimate:
    """Approximate work for analyzing ONE source version for the first time. Worst-case where the
    truth is unknown before reading the file: every likely-scanned page might need the vision model."""

    embed_tokens: int
    vision_pages: int
    figures: int
    calls: int
    cost_known: bool
    expected_cost_micros: int | None
    max_cost_micros: int | None


def estimate_analysis(
    settings: Settings,
    *,
    text_chars: int,
    likely_scanned_pages: int | None,
    image_count: int,
    kind: str,
) -> AnalysisEstimate:
    scanned = likely_scanned_pages or 0
    vision_pages = min(scanned, settings.ai_vision_fallback_max_pages) if kind == "pdf" else 0
    # figures: embedded pictures (DOCX/PDF) plus drawing pages we cannot count yet; capped
    figures = min(
        image_count, settings.ai_max_new_analysis_figures, settings.ai_max_figures_per_source
    )
    text_tokens = estimate_text_tokens(settings, "x" * text_chars) + scanned * 600
    chunks = max(1, math.ceil(text_tokens / max(1, settings.ai_chunk_target_tokens))) + figures
    embed_tokens = text_tokens + figures * 200
    calls = math.ceil(chunks / max(1, settings.ai_embed_batch_size)) + vision_pages + figures
    prices = Prices.of(settings)
    expected = worst = None
    if prices.llm_known or prices.embed_text is not None:
        embed_cost = prices.embed_text_cost(Usage(embed_tokens))
        page_in = settings.ai_image_tokens_estimate + 300
        expected = embed_cost + prices.llm_cost(Usage(page_in * vision_pages, 1500 * vision_pages))
        expected += prices.llm_cost(Usage((page_in + 100) * figures, 500 * figures))
        worst = expected + prices.llm_cost(
            Usage(0, (OCR_OUT_MAX - 1500) * vision_pages + (DESCRIBE_OUT_MAX - 500) * figures)
        )
    return AnalysisEstimate(
        embed_tokens=embed_tokens,
        vision_pages=vision_pages,
        figures=figures,
        calls=calls,
        cost_known=prices.llm_known or prices.embed_text is not None,
        expected_cost_micros=expected,
        max_cost_micros=worst,
    )


OCR_OUT_MAX = 4000
DESCRIBE_OUT_MAX = 1500
