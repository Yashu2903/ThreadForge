"""Move ONE failed outbox row back to pending.

    python -m app.commands.outbox_requeue --event-id <uuid>

Attempts, the last error code and the event identity are preserved. Only rows in state 'failed'
are touched; the event itself (room_events) is never modified. Fix the cause first (for a row that
failed with envelope_invalid, the stored envelope itself is wrong and requeuing will fail it again).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from app.config import ConfigError, load_database_url
from app.db import create_engine_from_url, create_sessionmaker
from app.services.outbox import requeue_failed


async def _run(event_id: uuid.UUID) -> int:
    engine = create_engine_from_url(load_database_url())
    try:
        moved = await requeue_failed(create_sessionmaker(engine), event_id)
    finally:
        await engine.dispose()
    if moved:
        print(f"requeued {event_id}: it is pending again and due now")
        return 0
    print(f"nothing requeued: no FAILED outbox row with event_id {event_id}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Requeue one failed outbox entry.")
    parser.add_argument("--event-id", required=True, type=uuid.UUID)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.event_id))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
