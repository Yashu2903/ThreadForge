"""Token estimates and cost arithmetic. All money is integer micro-dollars (1 USD = 1_000_000).

TOKEN COUNTING: no tokenizer for the configured models has been verified, so we use a
conservative estimate: ceil(characters / AI_CHARS_PER_TOKEN) * AI_TOKEN_SAFETY_MARGIN. English text
is about 4 characters per token; the default 3.0 over-counts on purpose, because the estimate guards a
budget and a hard pre-send limit (over-counting is safe, under-counting is not). When the provider
reports real usage, that replaces the estimate in the ledger.

PRICES are per MILLION tokens, rounded UP per call, so many tiny calls can never round to free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.ai.types import ImagePart, Part, TextPart, Usage
from app.config import Settings

MTOK = 1_000_000


def estimate_text_tokens(settings: Settings, text: str) -> int:
    if not text:
        return 0
    base = math.ceil(len(text) / settings.ai_chars_per_token)
    return math.ceil(base * settings.ai_token_safety_margin)


def estimate_parts_tokens(settings: Settings, parts: list[Part]) -> int:
    total = 0
    for part in parts:
        if isinstance(part, TextPart):
            total += estimate_text_tokens(settings, part.text)
        elif isinstance(part, ImagePart):
            total += settings.ai_image_tokens_estimate
    return total


def _ceil_div(value: int, divisor: int) -> int:
    return -(-value // divisor)


@dataclass(frozen=True)
class Prices:
    llm_input: int | None
    llm_output: int | None
    embed_text: int | None
    embed_image: int | None

    @classmethod
    def of(cls, settings: Settings) -> Prices:
        return cls(
            settings.ai_llm_input_micros_per_mtok,
            settings.ai_llm_output_micros_per_mtok,
            settings.ai_embed_text_micros_per_mtok,
            settings.ai_embed_image_micros_per_mtok,
        )

    @property
    def llm_known(self) -> bool:
        return self.llm_input is not None and self.llm_output is not None

    def llm_cost(self, usage: Usage) -> int:
        """Generation cost; image tokens are already part of input_tokens (billed at the input rate)."""
        if not self.llm_known:
            return 0
        assert self.llm_input is not None and self.llm_output is not None
        return _ceil_div(usage.input_tokens * self.llm_input, MTOK) + _ceil_div(
            usage.output_tokens * self.llm_output, MTOK
        )

    def embed_text_cost(self, usage: Usage) -> int:
        if self.embed_text is None:
            return 0
        return _ceil_div(usage.input_tokens * self.embed_text, MTOK)

    def embed_image_cost(self, usage: Usage) -> int:
        """Image embedding is billed per image token at its own rate; when that price is unset it
        falls back to the text rate (documented; the self-check status reports the fallback)."""
        rate = self.embed_image if self.embed_image is not None else self.embed_text
        if rate is None:
            return 0
        return _ceil_div(usage.input_tokens * rate, MTOK)
