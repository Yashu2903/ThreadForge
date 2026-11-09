"""The analysis worker's door to the providers.

One `AnalysisGateway` per job. It owns: the two plaintext API keys (loaded for exactly the key source
decided at admission, never the other one), the two adapters, the per-job meter (call and image caps
from the job's snapshot) and the `MeteredCaller`, so that EVERY call:

  * is refused before sending if it would break a cap;
  * reserves its worst-case cost against the server budgets (server key only);
  * is retried only for 429 / 5xx / timeouts, with backoff;
  * writes a ledger row (ok, failed, retried, cancelled) with the job id;
  * is abandoned, with its late result discarded, when the job was cancelled, lost its claim, or its
    source was removed (`is_cancelled` is checked before every call and between retries).

Failures leave this class as exactly two exceptions, so the pipeline has one thing to catch:
`AnalysisCancelled` (stop quietly, the job is no longer wanted) and `AnalysisAbort(code, retryable)`
(stop with a stable error code; `retryable` means "worth another attempt later").
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import errors as perr
from app.ai import keys
from app.ai.calls import CapExceeded, KeyLimiter, MeteredCaller, RequestMeter
from app.ai.factory import build_embedder, build_llm
from app.ai.pricing import Prices, estimate_parts_tokens, estimate_text_tokens
from app.ai.spend import BudgetExhausted, CallContext, Spend
from app.ai.types import ImagePart, Part, TextPart, Usage
from app.config import Settings
from app.errors import AppError
from app.models import AnalysisJob

T = TypeVar("T")
_JOBS = AnalysisJob.__table__
RETRYABLE_PROVIDER_CODES = {perr.RATE_LIMITED, perr.UNAVAILABLE, perr.TIMEOUT}
OCR_MAX_OUTPUT_TOKENS = 4000
DESCRIBE_MAX_OUTPUT_TOKENS = 1500

_limiter: KeyLimiter | None = None


def process_limiter(settings: Settings) -> KeyLimiter:
    """One limiter per worker process, so concurrent jobs share the per-key concurrency limit."""
    global _limiter
    if _limiter is None:
        _limiter = KeyLimiter(settings.ai_key_concurrency)
    return _limiter


class AnalysisCancelled(Exception):
    """The job is no longer wanted (cancelled, claim lost, source removed). Stop quietly."""


class AnalysisAbort(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AnalysisGateway:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        job: Any,  # an analysis_jobs row
        *,
        is_cancelled: Callable[[], Awaitable[bool]],
        transports: dict[str, httpx2.AsyncBaseTransport] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        mode: str = "analysis",
    ) -> None:
        self._sm = sessionmaker
        self._settings = settings
        self.settings = settings
        self._job = job
        self._is_cancelled = is_cancelled
        self._transports = transports
        self._prices = Prices.of(settings)
        self._mode = mode  # "analysis" (an analysis job) or "request" (an `@AI` request)
        if mode == "request":
            # the per-request caps of the settings: provider calls, input/output tokens, images
            self.meter = RequestMeter(settings)
        else:
            caps = job.caps or {}
            self.meter = RequestMeter(
                settings,
                max_calls=int(
                    caps.get("max_provider_calls", settings.ai_analysis_max_provider_calls)
                ),
                max_images=int(caps.get("max_images", 10_000)),
            )
        # what this run used, for the result's usage record (tokens as reported or estimated)
        self.totals = {
            "llm_calls": 0,
            "llm_in": 0,
            "llm_out": 0,
            "images": 0,
            "embed_calls": 0,
            "embed_tokens": 0,
        }
        self._caller = MeteredCaller(
            settings, Spend(sessionmaker, settings), process_limiter(settings), sleep
        )
        self._llm: Any = None
        self._embedder: Any = None
        self._llm_key = ""
        self._embed_key = ""

    # ---- setup ---------------------------------------------------------------------------------

    async def _llm_adapter(self) -> Any:
        if self._llm is None:
            key = await self._key("openai", self._job.key_source_generation)
            self._llm = build_llm(self._settings, key, self._transports)
            self._llm_key = key
        return self._llm

    async def _embed_adapter(self) -> Any:
        if self._embedder is None:
            key = await self._key("google", self._job.key_source_embedding)
            self._embedder = build_embedder(self._settings, key, self._transports)
            self._embed_key = key
        return self._embedder

    async def _key(self, provider: str, source: str) -> str:
        try:
            return await keys.load_api_key(
                self._sm,
                self._settings,
                self._job.charged_user_id,
                provider,
                source,  # type: ignore[arg-type]
            )
        except AppError as exc:
            raise AnalysisAbort(exc.code) from None

    def _count_embed(self, usage: Usage) -> Usage:
        self.totals["embed_calls"] += 1
        self.totals["embed_tokens"] += usage.input_tokens
        return usage

    async def aclose(self) -> None:
        for adapter in (self._llm, self._embedder):
            if adapter is not None:
                await adapter.aclose()

    def _ctx(self, provider: str, model: str, source: str) -> CallContext:
        return CallContext(
            user_id=self._job.requested_by,
            charged_user_id=self._job.charged_user_id,
            room_id=self._job.room_id,
            provider=provider,
            model=model,
            key_source=source,  # type: ignore[arg-type]
            analysis_job_id=self._job.id if self._mode == "analysis" else None,
            request_id=self._job.id if self._mode == "request" else None,
        )

    # ---- the one place errors are translated ---------------------------------------------------

    async def _run(
        self,
        provider: str,
        source: str,
        operation: str,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        usage_of: Callable[[T], Usage],
        cost_of: Callable[[Usage], int],
        worst_case: Usage,
        model: str,
    ) -> T:
        try:
            return await self._caller.call(
                self._ctx(provider, model, source),
                operation,
                key,
                fn,
                usage_of=usage_of,
                cost_of=cost_of,
                worst_case=worst_case,
                meter=self.meter,
                is_cancelled=self._is_cancelled,
            )
        except BudgetExhausted:
            raise AnalysisAbort("budget_exhausted") from None
        except CapExceeded:
            raise AnalysisAbort("analysis_cap_exceeded") from None
        except perr.ProviderError as exc:
            if exc.code == perr.CANCELLED:
                raise AnalysisCancelled from None
            if exc.code == perr.KEY_REJECTED:
                if source == "user":
                    await keys.mark_rejected(self._sm, self._job.charged_user_id, provider)
                raise AnalysisAbort("key_rejected") from None
            raise AnalysisAbort(exc.code, retryable=exc.code in RETRYABLE_PROVIDER_CODES) from None

    # ---- generation (vision) -------------------------------------------------------------------

    async def structured(
        self,
        operation: str,
        parts: list[Part],
        *,
        system: str,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> Any:
        """One structured-output generation call (text and/or images in `parts`), through the call
        policy. Returns the parsed JSON."""
        llm = await self._llm_adapter()
        est_in = estimate_parts_tokens(self._settings, parts) + estimate_text_tokens(
            self._settings, system
        )
        images = sum(1 for p in parts if isinstance(p, ImagePart))

        def track(result):
            self.totals["llm_calls"] += 1
            self.totals["llm_in"] += result.usage.input_tokens
            self.totals["llm_out"] += result.usage.output_tokens
            self.totals["images"] += images
            return result.usage

        result = await self._run(
            "openai",
            self._job.key_source_generation,
            operation,
            self._llm_key,
            lambda: llm.generate_structured(
                system=system,
                parts=parts,
                schema=schema,
                schema_name=schema_name,
                max_output_tokens=max_output_tokens,
            ),
            usage_of=track,
            cost_of=self._prices.llm_cost,
            worst_case=Usage(est_in, max_output_tokens, images),
            model=llm.model,
        )
        return result.data

    async def vision_structured(
        self,
        operation: str,
        image_png: bytes,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> Any:
        return await self.structured(
            operation,
            [TextPart(user), ImagePart(image_png, "image/png")],
            system=system,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
        )

    # ---- embeddings ----------------------------------------------------------------------------

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Documents, batched; one ledgered call per batch. Same order as the input."""
        embedder = await self._embed_adapter()
        out: list[list[float]] = []
        size = self._settings.ai_embed_batch_size
        for start in range(0, len(texts), size):
            batch = texts[start : start + size]
            prefix = self._settings.ai_embed_document_prefix
            est = sum(estimate_text_tokens(self._settings, prefix + t) for t in batch)
            result = await self._run(
                "google",
                self._job.key_source_embedding,
                "embed_text",
                self._embed_key,
                lambda b=batch: embedder.embed_texts(b),
                usage_of=lambda r: r.usage,
                cost_of=self._prices.embed_text_cost,
                worst_case=Usage(math.ceil(est * 1.0), 0, 0, estimated=True),
                model=embedder.model,
            )
            out += result.vectors
        return out

    async def embed_query(self, query: str) -> list[list[float]]:
        """The query text as a QUERY (the task prefix differs from the one for documents)."""
        embedder = await self._embed_adapter()
        prefixed = self._settings.ai_embed_query_prefix + query
        tokens = estimate_text_tokens(self._settings, prefixed)
        result = await self._run(
            "google",
            self._job.key_source_embedding,
            "embed_text",
            self._embed_key,
            lambda: embedder.embed_query(query),
            usage_of=lambda r: self._count_embed(r.usage),
            cost_of=self._prices.embed_text_cost,
            worst_case=Usage(tokens, 0, 0, estimated=True),
            model=embedder.model,
        )
        return result.vectors

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        embedder = await self._embed_adapter()
        out: list[list[float]] = []
        size = max(1, min(self._settings.ai_embed_batch_size, 6))  # the docs: up to 6 per request
        for start in range(0, len(images), size):
            batch = [ImagePart(b, "image/png") for b in images[start : start + size]]
            tokens = len(batch) * self._settings.ai_embed_image_tokens_estimate
            result = await self._run(
                "google",
                self._job.key_source_embedding,
                "embed_image",
                self._embed_key,
                lambda b=batch: embedder.embed_images(b),
                usage_of=lambda r: r.usage,
                cost_of=self._prices.embed_image_cost,
                worst_case=Usage(tokens, 0, len(batch), estimated=True),
                model=embedder.model,
            )
            out += result.vectors
        return out

    async def embed_interleaved(self, items: list[tuple[str, bytes]]) -> list[list[float]]:
        embedder = await self._embed_adapter()
        out: list[list[float]] = []
        size = max(1, min(self._settings.ai_embed_batch_size, 6))
        for start in range(0, len(items), size):
            batch = [(t, ImagePart(b, "image/png")) for t, b in items[start : start + size]]
            tokens = sum(
                estimate_text_tokens(self._settings, self._settings.ai_embed_document_prefix + t)
                + self._settings.ai_embed_image_tokens_estimate
                for t, _ in batch
            )
            result = await self._run(
                "google",
                self._job.key_source_embedding,
                "embed_image",
                self._embed_key,
                lambda b=batch: embedder.embed_interleaved(b),
                usage_of=lambda r: r.usage,
                cost_of=self._prices.embed_image_cost,
                worst_case=Usage(tokens, 0, len(batch), estimated=True),
                model=embedder.model,
            )
            out += result.vectors
        return out
