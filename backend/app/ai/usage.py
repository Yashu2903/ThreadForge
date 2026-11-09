"""The owner's usage page: spend by day, user, operation and key source for ONE room, the budget
windows that apply, and the warnings that fired. Read-only aggregates; never any content."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.spend import today_utc
from app.config import Settings

_GROUPS = {
    "by_day": "to_char((l.created_at AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD')",
    "by_user": "COALESCE(u.display_name, '(system)')",
    "by_operation": "l.operation",
    "by_key_source": "l.key_source",
}
_AGG = """
    count(*) AS calls,
    count(*) FILTER (WHERE l.status = 'ok') AS ok_calls,
    count(*) FILTER (WHERE l.status = 'failed') AS failed_calls,
    COALESCE(sum(l.input_tokens), 0) AS input_tokens,
    COALESCE(sum(l.output_tokens), 0) AS output_tokens,
    COALESCE(sum(l.image_count), 0) AS images,
    COALESCE(sum(l.est_cost_micros), 0) AS cost_micros
"""


async def usage_for_room(
    db: AsyncSession, settings: Settings, room_id: uuid.UUID, days: int
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    params = {"room": room_id, "since": since}
    out: dict[str, Any] = {"days": days, "prices_configured": settings.ai_prices_configured}
    total = (
        (
            await db.execute(
                text(
                    f"SELECT {_AGG} FROM ai_usage_ledger l WHERE l.room_id = :room AND l.created_at >= :since"
                ),
                params,
            )
        )
        .mappings()
        .one()
    )
    out["totals"] = {k: int(v) for k, v in total.items()}
    for name, expr in _GROUPS.items():
        order = "1" if name == "by_day" else "cost_micros DESC, calls DESC"
        rows = (
            (
                await db.execute(
                    text(
                        f"SELECT {expr} AS label, {_AGG} FROM ai_usage_ledger l "
                        "LEFT JOIN users u ON u.id = l.charged_user_id "
                        f"WHERE l.room_id = :room AND l.created_at >= :since GROUP BY 1 ORDER BY {order}"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        out[name] = [{k: (int(v) if k != "label" else v) for k, v in r.items()} for r in rows]

    day = today_utc()
    limits = {
        ("global", "", day.replace(day=1)): settings.ai_budget_global_monthly_micros,
        ("room", str(room_id), day): settings.ai_budget_room_daily_micros,
    }
    budgets = []
    for (scope, key, window), limit in limits.items():
        if limit is None:
            continue
        row = (
            await db.execute(
                text(
                    "SELECT spent_micros, reserved_micros FROM ai_budget_windows "
                    "WHERE scope = :s AND scope_key = :k AND window_start = :w"
                ),
                {"s": scope, "k": key, "w": window},
            )
        ).one_or_none()
        spent = int(row.spent_micros) if row else 0
        reserved = int(row.reserved_micros) if row else 0
        budgets.append(
            {
                "scope": scope,
                "window_start": window.isoformat(),
                "limit_micros": limit,
                "spent_micros": spent,
                "reserved_micros": reserved,
                "percent": round(100 * spent / limit, 1) if limit else 100.0,
            }
        )
    out["budgets"] = budgets
    warnings = (
        await db.execute(
            text(
                "SELECT scope, window_start, threshold, created_at FROM ai_budget_warnings "
                "WHERE (scope = 'global' AND window_start >= :gm) "
                "   OR (scope = 'room' AND scope_key = :room AND window_start >= :since_day) "
                "ORDER BY created_at DESC LIMIT 20"
            ),
            {"gm": day.replace(day=1), "room": str(room_id), "since_day": since.date()},
        )
    ).all()
    out["warnings"] = [
        {
            "scope": w.scope,
            "window_start": w.window_start.isoformat(),
            "threshold_percent": w.threshold,
            "at": w.created_at.isoformat(),
        }
        for w in warnings
    ]
    return out
