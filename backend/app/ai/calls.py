"""The call policy: every provider call in the system goes through `MeteredCaller.call`.

For one logical call it does, in this order:
  1. refuse if the request already used its allowed number of provider calls (a per-request cap);
  2. refuse if the request was cancelled;
  3. for SERVER-key calls, reserve the worst-case cost against the budgets (app/ai/spend.py);
  4. take a slot in the per-key concurrency limiter, make the HTTP call, give the slot back;
  5. on success write a ledger row with the real usage and settle the reservation;
  6. on a retryable failure (429, 5xx, timeout) write a FAILED ledger row, wait (capped exponential
     backoff with jitter, or the provider's Retry-After), and try again, up to AI_RETRY_MAX_ATTEMPTS;
  7. on a non-retryable failure (400, 401, 403, refusal, bad output) write a FAILED ledger row,
     release the reservation and raise. Nothing is retried that cannot get better.

No database transaction is open while the HTTP call runs: each ledger row / reservation uses its own
short transaction before or after the call.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.ai import errors
from app.ai.spend import BudgetExhausted, CallContext, Reservation, Spend
from app.ai.types import Usage
from app.config import Settings

T = TypeVar("T")


class CapExceeded(Exception):
    def __init__(self, cap: str) -> None:
        super().__init__(cap)
        self.cap = cap


class RequestMeter:
    """Counts the provider calls, input tokens and images one request is allowed to use."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls = 0
        self.images = 0

    def before_call(self, input_tokens: int, output_tokens: int, images: int) -> None:
        s = self.settings
        if self.calls + 1 > s.ai_max_provider_calls:
            raise CapExceeded("max_provider_calls")
        if input_tokens > s.ai_max_input_tokens:
            raise CapExceeded("max_input_tokens")
        if output_tokens > s.ai_max_output_tokens:
            raise CapExceeded("max_output_tokens")
        if self.images + images > s.ai_max_images:
            raise CapExceeded("max_images")
        self.calls += 1
        self.images += images


class KeyLimiter:
    """At most N simultaneous provider calls per key (per process). The key is identified by a hash
    prefix so the map never holds the secret itself."""

    def __init__(self, per_key: int) -> None:
        self._per_key = per_key
        self._slots: dict[str, asyncio.Semaphore] = {}

    def slot(self, provider: str, api_key: str) -> asyncio.Semaphore:
        ident = provider + ":" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
        return self._slots.setdefault(ident, asyncio.Semaphore(self._per_key))


def backoff_delay(settings: Settings, attempt: int, retry_after: float | None) -> float:
    """attempt is 1-based (the attempt that just failed). Full jitter between 50% and 100%."""
    exp = min(
        settings.ai_retry_max_delay_seconds,
        settings.ai_retry_base_delay_seconds * 2 ** (attempt - 1),
    )
    delay = exp * random.uniform(0.5, 1.0)  # noqa: S311 - jitter, not security
    if retry_after is not None:
        delay = max(delay, min(retry_after, 120.0))
    return delay


class MeteredCaller:
    def __init__(
        self,
        settings: Settings,
        spend: Spend,
        limiter: KeyLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._spend = spend
        self._limiter = limiter or KeyLimiter(settings.ai_key_concurrency)
        self._sleep = sleep

    async def call(
        self,
        ctx: CallContext,
        operation: str,
        api_key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        usage_of: Callable[[T], Usage],
        cost_of: Callable[[Usage], int],
        worst_case: Usage,
        meter: RequestMeter | None = None,
        is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    ) -> T:
        """Raises ProviderError, BudgetExhausted, CapExceeded or CancelledByRequest."""
        if meter is not None:
            meter.before_call(
                worst_case.input_tokens, worst_case.output_tokens, worst_case.image_count
            )
        if is_cancelled is not None and await is_cancelled():
            await self._spend.record(
                ctx, operation, Usage(), 0, "cancelled", error_code=errors.CANCELLED
            )
            raise errors.ProviderError(errors.CANCELLED, "cancelled")
        reservation: Reservation | None = await self._spend.reserve(ctx, cost_of(worst_case))
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    async with self._limiter.slot(ctx.provider, api_key):
                        result = await fn()
                except errors.ProviderError as exc:
                    await self._spend.record(
                        ctx, operation, Usage(), 0, "failed", error_code=exc.code, attempt=attempt
                    )
                    if not exc.retryable or attempt >= self._settings.ai_retry_max_attempts:
                        await self._spend.release(reservation)
                        reservation = None
                        raise
                    await self._sleep(backoff_delay(self._settings, attempt, exc.retry_after))
                    if is_cancelled is not None and await is_cancelled():
                        await self._spend.record(
                            ctx,
                            operation,
                            Usage(),
                            0,
                            "cancelled",
                            error_code=errors.CANCELLED,
                            attempt=attempt,
                        )
                        await self._spend.release(reservation)
                        reservation = None
                        raise errors.ProviderError(errors.CANCELLED, "cancelled") from None
                    continue
                usage = usage_of(result)
                cost = cost_of(usage)
                await self._spend.record(ctx, operation, usage, cost, "ok", attempt=attempt)
                await self._spend.settle(reservation, cost)
                reservation = None
                return result
        except asyncio.CancelledError:
            # the worker task itself was cancelled: give the reservation back, then let it propagate
            await asyncio.shield(self._spend.release(reservation))
            raise
        except BudgetExhausted:
            raise
