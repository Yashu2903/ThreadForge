"""Operator view of AI health. No content, no secrets: counts, codes and money only.

    python -m app.commands.ai_status

Shows the global switch, capability self-check results, failures by code over 24 h, spend today and
this month (server key vs user key), and the analysis queue: jobs by state, the age of the oldest
waiting one, and job failures by code over 24 h. (The request queue joins in checkpoint 4.)
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


_QUEUE = text(
    """
    SELECT state, count(*) AS n, min(created_at) AS oldest
      FROM analysis_jobs WHERE state IN ('queued', 'running') GROUP BY state ORDER BY state
    """
)
_JOB_FAILURES = text(
    """
    SELECT error_code, count(*) AS n FROM analysis_jobs
     WHERE state = 'failed' AND finished_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC
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
            queue = (await db.execute(_QUEUE)).all()
            job_failures = (await db.execute(_JOB_FAILURES)).all()
            now = (await db.execute(text("SELECT now()"))).scalar_one()
    finally:
        await engine.dispose()
    return {
        "global_switch": "on" if flag.ai_enabled else "OFF",
        "note": flag.note,
        "capabilities": [(c.capability, "ok" if c.ok else c.code, c.checked_at) for c in caps],
        "failures_24h": [(f.error_code or "-", f.n) for f in failures],
        "spend_micros": [(s.key_source, int(s.today), int(s.month)) for s in spend],
        "analysis_queue": [
            (q.state, q.n, int((now - q.oldest).total_seconds()) if q.oldest else 0) for q in queue
        ],
        "analysis_failures_24h": [(f.error_code or "-", f.n) for f in job_failures],
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
    queue = ", ".join(f"{state}={n} (oldest {age}s)" for state, n, age in info["analysis_queue"])
    print(f"analysis queue: {queue or 'empty'}")
    job_failures = ", ".join(f"{code}={n}" for code, n in info["analysis_failures_24h"]) or "none"
    print(f"job failures  : {job_failures} (24 h)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
