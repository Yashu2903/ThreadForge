"""Show the state of the transactional outbox.   python -m app.commands.outbox_status

Counts, ages, retry numbers, and a list of FAILED entries (event ids and error codes only). It never
selects payloads, so nothing sensitive can be printed. Suggested alert (documentation only): the
oldest pending row is older than two minutes.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import ConfigError, load_database_url
from app.db import create_engine_from_url, create_sessionmaker
from app.services.outbox import outbox_stats

ALERT_AFTER_SECONDS = 120


async def _run() -> int:
    engine = create_engine_from_url(load_database_url())
    try:
        stats = await outbox_stats(create_sessionmaker(engine))
    finally:
        await engine.dispose()

    age = stats.oldest_pending_age_seconds
    print(f"pending:                 {stats.pending} (due now: {stats.pending_due})")
    print(f"oldest pending age:      {'-' if age is None else f'{age:.1f}s'}")
    print(f"processing (claimed):    {stats.processing}")
    print(f"retrying (pending, previously claimed): {stats.retrying}")
    print(f"highest attempt count:   {stats.max_attempts}")
    print(f"failed:                  {stats.failed}")
    for event_id, code, attempts in stats.failed_entries:
        print(f"  failed event_id={event_id} error={code} attempts={attempts}")
    if stats.failed_entries:
        print("  requeue one with: python -m app.commands.outbox_requeue --event-id <uuid>")
    if age is not None and age > ALERT_AFTER_SECONDS:
        print(
            f"ALERT: oldest pending row is older than {ALERT_AFTER_SECONDS}s "
            "(is the dispatcher running?)"
        )
        return 1
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
