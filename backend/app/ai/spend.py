"""The usage ledger and budget reservations.

TERMS
  * ledger     an append-only table (ai_usage_ledger) with one row per provider call attempt.
  * budget     a limit on SERVER-key spend in one window: global per month, per user per day, per room
               per day. Calls made with a user's own key are not counted against these.
  * reserve    before a call, set aside the call's worst-case cost in every applicable window. The set
               aside amount counts as used, so two concurrent callers cannot both spend the last
               dollar. After the call: `settle` (replace the reservation with the real cost) or
               `release` (give it back, the call failed).

THE GUARDED UPSERT (the atomic part). One SQL statement per window:

    INSERT INTO ai_budget_windows (...) SELECT ..., :est WHERE :est <= :limit
    ON CONFLICT (scope, scope_key, window_start) DO UPDATE
        SET reserved_micros = reserved_micros + :est
        WHERE spent_micros + reserved_micros + :est <= :limit
    RETURNING ...

If the window row does not exist it is created (only if the estimate alone fits). If it exists, the
UPDATE's WHERE clause is evaluated against the row version PostgreSQL has just locked for us, so two
concurrent reservations queue on the row lock and the second one sees the first one's reservation.
No row returned = the budget would be exceeded = refuse. All windows of one reservation are taken in
ONE transaction in a fixed order (global, user, room), so a refusal in any window rolls back the others
and concurrent reservations cannot deadlock.

Overshoot: only by (actual - estimate) when a call turns out dearer than its worst-case estimate.
Estimates use max_output_tokens and a conservative token count, so this should not occur; if prices are
misconfigured too low, spend control is lost (documented in docs/ai.md).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.types import KeySource, Usage
from app.config import Settings
from app.logging_config import APP_LOGGER
from app.models import AiUsageLedger

log = logging.getLogger(f"{APP_LOGGER}.ai.spend")
THRESHOLDS = (50, 80, 100)


class BudgetExhausted(Exception):
    def __init__(self, scope: str) -> None:
        super().__init__(scope)
        self.scope = scope  # global | user | room


@dataclass(frozen=True)
class CallContext:
    """Who is making a provider call, and on whose account."""

    user_id: uuid.UUID | None  # the requester (None only for the operator self-check)
    charged_user_id: uuid.UUID | None  # whose budget / key (same as user_id in phase 1)
    room_id: uuid.UUID | None
    provider: str  # openai | google
    model: str
    key_source: KeySource
    request_id: uuid.UUID | None = None
    analysis_job_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Window:
    scope: str
    scope_key: str
    window_start: date
    limit: int


@dataclass(frozen=True)
class Reservation:
    windows: tuple[Window, ...]
    micros: int


def today_utc(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).date()


def windows_for(settings: Settings, ctx: CallContext, now: datetime | None = None) -> list[Window]:
    """The budget windows a SERVER-key call is counted against (empty = none configured)."""
    day = today_utc(now)
    out: list[Window] = []
    if settings.ai_budget_global_monthly_micros is not None:
        out.append(
            Window("global", "", day.replace(day=1), settings.ai_budget_global_monthly_micros)
        )
    if settings.ai_budget_user_daily_micros is not None and ctx.charged_user_id is not None:
        out.append(
            Window("user", str(ctx.charged_user_id), day, settings.ai_budget_user_daily_micros)
        )
    if settings.ai_budget_room_daily_micros is not None and ctx.room_id is not None:
        out.append(Window("room", str(ctx.room_id), day, settings.ai_budget_room_daily_micros))
    return out


_RESERVE = text(
    """
    INSERT INTO ai_budget_windows AS w (scope, scope_key, window_start, reserved_micros)
    SELECT :scope, :key, :ws, CAST(:est AS bigint) WHERE CAST(:est AS bigint) <= CAST(:lim AS bigint)
    ON CONFLICT (scope, scope_key, window_start) DO UPDATE
       SET reserved_micros = w.reserved_micros + CAST(:est AS bigint), updated_at = now()
       WHERE w.spent_micros + w.reserved_micros + CAST(:est AS bigint) <= CAST(:lim AS bigint)
    RETURNING w.reserved_micros
    """
)
_SETTLE = text(
    """
    INSERT INTO ai_budget_windows AS w (scope, scope_key, window_start, spent_micros)
    VALUES (:scope, :key, :ws, CAST(:actual AS bigint))
    ON CONFLICT (scope, scope_key, window_start) DO UPDATE
       SET reserved_micros = GREATEST(w.reserved_micros - CAST(:est AS bigint), 0),
           spent_micros = w.spent_micros + CAST(:actual AS bigint), updated_at = now()
    RETURNING w.spent_micros
    """
)
_RELEASE = text(
    """
    UPDATE ai_budget_windows
       SET reserved_micros = GREATEST(reserved_micros - CAST(:est AS bigint), 0), updated_at = now()
     WHERE scope = :scope AND scope_key = :key AND window_start = :ws
    """
)
_WARN = text(
    """
    INSERT INTO ai_budget_warnings (scope, scope_key, window_start, threshold)
    VALUES (:scope, :key, :ws, :th) ON CONFLICT DO NOTHING RETURNING threshold
    """
)


class Spend:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sm = sessionmaker
        self._settings = settings

    # ---- ledger ------------------------------------------------------------------------------

    async def record(
        self,
        ctx: CallContext,
        operation: str,
        usage: Usage,
        cost_micros: int,
        status: str,
        *,
        error_code: str | None = None,
        attempt: int = 1,
    ) -> None:
        """One ledger row, in its OWN short transaction (never held open across a provider call)."""
        async with self._sm() as db, db.begin():
            await db.execute(
                insert(AiUsageLedger.__table__).values(
                    request_id=ctx.request_id,
                    analysis_job_id=ctx.analysis_job_id,
                    user_id=ctx.user_id,
                    charged_user_id=ctx.charged_user_id,
                    room_id=ctx.room_id,
                    provider=ctx.provider,
                    model=ctx.model,
                    operation=operation,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    image_count=usage.image_count,
                    est_cost_micros=cost_micros,
                    key_source=ctx.key_source,
                    status=status,
                    error_code=error_code,
                    attempt=attempt,
                    usage_estimated=usage.estimated,
                )
            )

    # ---- reservation -------------------------------------------------------------------------

    async def reserve(self, ctx: CallContext, est_micros: int) -> Reservation | None:
        """None when nothing needs reserving (user key, no budgets, or a free call)."""
        if ctx.key_source != "server" or est_micros <= 0:
            return None
        windows = windows_for(self._settings, ctx)
        if not windows:
            return None
        async with self._sm() as db, db.begin():
            for w in windows:  # fixed order: global, user, room
                row = (
                    await db.execute(
                        _RESERVE,
                        {
                            "scope": w.scope,
                            "key": w.scope_key,
                            "ws": w.window_start,
                            "est": est_micros,
                            "lim": w.limit,
                        },
                    )
                ).first()
                if row is None:
                    # leaving the block rolls back the windows already taken in this transaction
                    raise BudgetExhausted(w.scope)
        return Reservation(tuple(windows), est_micros)

    async def settle(self, reservation: Reservation | None, actual_micros: int) -> None:
        if reservation is None:
            return
        fired: list[tuple[Window, int]] = []
        async with self._sm() as db, db.begin():
            for w in reservation.windows:
                spent = (
                    await db.execute(
                        _SETTLE,
                        {
                            "scope": w.scope,
                            "key": w.scope_key,
                            "ws": w.window_start,
                            "est": reservation.micros,
                            "actual": actual_micros,
                        },
                    )
                ).scalar_one()
                fired += await self._warnings(db, w, spent)
        for w, threshold in fired:
            log.warning(
                "ai budget warning scope=%s window=%s threshold=%s%%",
                w.scope,
                w.window_start,
                threshold,
            )

    async def release(self, reservation: Reservation | None) -> None:
        if reservation is None:
            return
        async with self._sm() as db, db.begin():
            for w in reservation.windows:
                await db.execute(
                    _RELEASE,
                    {
                        "scope": w.scope,
                        "key": w.scope_key,
                        "ws": w.window_start,
                        "est": reservation.micros,
                    },
                )

    async def _warnings(self, db: AsyncSession, w: Window, spent: int) -> list[tuple[Window, int]]:
        """Insert each crossed threshold once per window; return the ones newly inserted."""
        out: list[tuple[Window, int]] = []
        if w.limit <= 0:
            return out
        for threshold in THRESHOLDS:
            if spent * 100 >= w.limit * threshold:
                inserted = (
                    await db.execute(
                        _WARN,
                        {
                            "scope": w.scope,
                            "key": w.scope_key,
                            "ws": w.window_start,
                            "th": threshold,
                        },
                    )
                ).first()
                if inserted is not None:
                    out.append((w, threshold))
        return out
