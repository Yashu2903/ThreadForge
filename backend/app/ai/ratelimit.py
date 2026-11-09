"""Fixed-window counters in PostgreSQL (the same idea as the chat write buckets, but generic).

Used to stop API-key *testing*: every key-validation attempt counts, success or failure, per user and
per IP. The counter commits in its own transaction BEFORE the provider is called, so a crash or an
exception afterwards cannot refund the attempt.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import rate_limited

_BUMP = text(
    """
    INSERT INTO ai_rate_buckets AS b (bucket, window_start, count) VALUES (:bucket, :ws, 1)
    ON CONFLICT (bucket, window_start) DO UPDATE SET count = b.count + 1
    RETURNING b.count
    """
)


async def consume(
    sessionmaker: async_sessionmaker[AsyncSession],
    bucket: str,
    limit: int,
    window_seconds: int = 3600,
    message: str = "Too many attempts. Try again later.",
) -> None:
    now = time.time()
    start = math.floor(now / window_seconds) * window_seconds
    async with sessionmaker() as db, db.begin():
        count = (
            await db.execute(
                _BUMP, {"bucket": bucket[:96], "ws": datetime.fromtimestamp(start, UTC)}
            )
        ).scalar_one()
    if count > limit:
        raise rate_limited(max(1, int(start + window_seconds - now)), message)
