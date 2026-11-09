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
