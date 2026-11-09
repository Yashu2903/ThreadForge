"""Operator view of AI health. No content, no secrets: counts, codes and money only.

    python -m app.commands.ai_status

Shows the global switch, capability self-check results, failures by code over 24 h, and spend today
and this month (server key vs user key). Queue depth and oldest-job age are reported once the
request/analysis queues exist (checkpoints 3-4); until then they are absent, not zero.
Needs only DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from sqlalchemy import text

from app.config import ConfigError, load_database_url
from app.db import create_engine_from_url, create_sessionmaker

_SPEND = text(
    """
    SELECT key_source,
      COALESCE(sum(est_cost_micros) FILTER (WHERE created_at >= date_trunc('day', now())), 0) AS today,
      COALESCE(sum(est_cost_micros) FILTER (WHERE created_at >= date_trunc('month', now())), 0) AS month
    FROM ai_usage_ledger GROUP BY 1 ORDER BY 1
    """
)
_FAILURES = text(
    """
    SELECT error_code, count(*) AS n FROM ai_usage_ledger
    WHERE status = 'failed' AND created_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC
    """
)


async def collect() -> dict[str, Any]:
    engine = create_engine_from_url(load_database_url())
    try:
        async with create_sessionmaker(engine)() as db:
            flag = (await db.execute(text("SELECT ai_enabled, note FROM ai_global_flags"))).one()
            caps = (
                await db.execute(
                    text(
                        "SELECT capability, ok, code, checked_at FROM ai_capability_status ORDER BY 1"
                    )
                )
            ).all()
            failures = (await db.execute(_FAILURES)).all()
            spend = (await db.execute(_SPEND)).all()
    finally:
        await engine.dispose()
    return {
        "global_switch": "on" if flag.ai_enabled else "OFF",
        "note": flag.note,
        "capabilities": [(c.capability, "ok" if c.ok else c.code, c.checked_at) for c in caps],
        "failures_24h": [(f.error_code or "-", f.n) for f in failures],
        "spend_micros": [(s.key_source, int(s.today), int(s.month)) for s in spend],
    }


def main(argv: list[str] | None = None) -> int:
    try:
        info = asyncio.run(collect())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"global switch : {info['global_switch']}  {info['note']}")
    print("capabilities  :")
    for name, state, at in info["capabilities"]:
        print(f"  {name:18} {state:22} {at:%Y-%m-%d %H:%M}")
    failures = ", ".join(f"{code}={n}" for code, n in info["failures_24h"]) or "none"
    print(f"failures 24 h : {failures}")
    for source, today, month in info["spend_micros"]:
        print(f"spend {source:6}: today ${today / 1e6:.4f}  month ${month / 1e6:.4f}")
    print("queue         : not available yet (no queue tables before checkpoint 3)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
