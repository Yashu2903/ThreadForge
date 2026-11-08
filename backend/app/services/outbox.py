"""Outbox operations for the dispatcher and the operator commands.

Every function here is ONE short transaction on its own session (the repository's pattern), and no
network call ever happens inside one. The dispatcher claims rows (committed), publishes to Redis
with NO transaction open, then records the outcome in a fresh transaction.

CLAIMING is a lease, not a lock held across the publish:
  * one statement picks eligible rows with FOR UPDATE SKIP LOCKED (two concurrent claimers skip
    each other's rows instead of waiting), marks them 'processing' with a fresh claim_token and
    claimed_until = now() + lease, bumps attempts, and returns them; then it commits;
  * a worker that dies leaves 'processing' rows whose claimed_until passes; they become eligible
    again (state = 'processing' AND claimed_until < now()), so pending work is never erased;
  * EVERY later update is token-matched (WHERE claim_token = :token AND state = 'processing'): a
    stale worker, whose lease expired and whose rows were re-claimed, matches zero rows and so can
    never overwrite a newer claim or completion.
All time comparisons use the DATABASE clock.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BACKOFF_BASE_SECONDS = 0.5
BACKOFF_FACTOR = 2.0


@dataclass(frozen=True)
class ClaimedRow:
    event_id: uuid.UUID
    room_id: uuid.UUID
    room_seq: int
    payload: dict[str, Any]
    attempts: int
    token: uuid.UUID


CLAIM_SQL = text(
    """
    WITH picked AS (
        SELECT event_id FROM outbox
        WHERE (state = 'pending' AND next_attempt_at <= now())
           OR (state = 'processing' AND claimed_until < now())
        ORDER BY next_attempt_at, created_at
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    )
    UPDATE outbox AS o
    SET state = 'processing',
        claim_token = :token,
        claimed_until = now() + make_interval(secs => CAST(:lease AS double precision)),
        attempts = o.attempts + 1
    FROM picked
    WHERE o.event_id = picked.event_id
    RETURNING o.event_id, o.room_id, o.room_seq, o.payload, o.attempts
    """
)


async def claim_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    batch_size: int,
    lease_seconds: float,
    token: uuid.UUID | None = None,
) -> list[ClaimedRow]:
    """Claim up to batch_size due rows with ONE fresh claim_token. Commits before returning."""
    token = token or uuid.uuid4()
    async with sessionmaker() as db, db.begin():
        result = await db.execute(
            CLAIM_SQL, {"batch": batch_size, "token": token, "lease": lease_seconds}
        )
        rows = result.all()
    claimed = [
        ClaimedRow(r.event_id, r.room_id, r.room_seq, r.payload, r.attempts, token) for r in rows
    ]
    claimed.sort(key=lambda c: (str(c.room_id), c.room_seq))  # tidy only: no order is promised
    return claimed


async def mark_processed(
    sessionmaker: async_sessionmaker[AsyncSession], event_id: uuid.UUID, token: uuid.UUID
) -> bool:
    """Token-matched success. False (zero rows) means the claim was stale: log it and move on."""
    async with sessionmaker() as db, db.begin():
        result = await db.execute(
            text(
                "UPDATE outbox SET state = 'processed', processed_at = now(),"
                " claim_token = NULL, claimed_until = NULL"
                " WHERE event_id = :id AND claim_token = :token AND state = 'processing'"
            ),
            {"id": event_id, "token": token},
        )
    return result.rowcount == 1


async def mark_retry(
    sessionmaker: async_sessionmaker[AsyncSession],
    event_id: uuid.UUID,
    token: uuid.UUID,
    error_code: str,
    delay_seconds: float,
) -> bool:
    """Token-matched transient failure: back to pending, claim cleared, retry scheduled."""
    async with sessionmaker() as db, db.begin():
        result = await db.execute(
            text(
                "UPDATE outbox SET state = 'pending', claim_token = NULL, claimed_until = NULL,"
                " last_error_code = :code,"
                " next_attempt_at = now() + make_interval(secs => CAST(:delay AS double precision))"
                " WHERE event_id = :id AND claim_token = :token AND state = 'processing'"
            ),
            {"id": event_id, "token": token, "code": error_code, "delay": delay_seconds},
        )
    return result.rowcount == 1


async def mark_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    event_id: uuid.UUID,
    token: uuid.UUID,
    error_code: str,
) -> bool:
    """Token-matched permanent failure (for example an envelope that fails validation)."""
    async with sessionmaker() as db, db.begin():
        result = await db.execute(
            text(
                "UPDATE outbox SET state = 'failed', claim_token = NULL, claimed_until = NULL,"
                " last_error_code = :code"
                " WHERE event_id = :id AND claim_token = :token AND state = 'processing'"
            ),
            {"id": event_id, "token": token, "code": error_code},
        )
    return result.rowcount == 1


async def release_claims(
    sessionmaker: async_sessionmaker[AsyncSession], token: uuid.UUID, event_ids: list[uuid.UUID]
) -> int:
    """Give unprocessed rows of THIS claim straight back to pending (token-matched), so another
    worker can take them now instead of waiting for the lease to expire (which risks a double
    publish if this worker is merely slow)."""
    if not event_ids:
        return 0
    async with sessionmaker() as db, db.begin():
        result = await db.execute(
            text(
                "UPDATE outbox SET state = 'pending', claim_token = NULL, claimed_until = NULL"
                " WHERE claim_token = :token AND state = 'processing'"
                " AND event_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"token": token, "ids": event_ids},
        )
    return result.rowcount


def retry_delay(attempts: int, max_delay_seconds: float, rng: random.Random | None = None) -> float:
    """Exponential backoff with EQUAL jitter.

    cap = min(max_delay, 0.5 s * 2^(attempts - 1)); delay = cap/2 (fixed half) + uniform(0, cap/2).
    So the delay is always in [cap/2, cap]: bounded above by the cap, never immediate (>= 0.25 s).
    `attempts` is the claim count, so the first retry (attempts = 1) waits 0.25-0.5 s.
    """
    rng = rng or random
    exponent = min(max(attempts, 1) - 1, 40)
    cap = min(max_delay_seconds, BACKOFF_BASE_SECONDS * BACKOFF_FACTOR**exponent)
    return cap / 2 + rng.random() * (cap / 2)


# ---- operator views ----------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxStats:
    pending: int
    pending_due: int
    oldest_pending_age_seconds: float | None
    processing: int
    failed: int
    retrying: int  # pending rows already claimed at least once
    max_attempts: int
    failed_entries: list[tuple[uuid.UUID, str, int]]  # (event_id, error code, attempts)


async def outbox_stats(
    sessionmaker: async_sessionmaker[AsyncSession], *, failed_limit: int = 50
) -> OutboxStats:
    """Counts and ages only. Never selects payloads."""
    async with sessionmaker() as db:
        summary = (
            await db.execute(
                text(
                    "SELECT"
                    " count(*) FILTER (WHERE state = 'pending') AS pending,"
                    " count(*) FILTER (WHERE state = 'pending'"
                    "   AND next_attempt_at <= now()) AS due,"
                    " extract(epoch FROM now() - min(created_at) FILTER (WHERE state = 'pending'))"
                    "   AS oldest_age,"
                    " count(*) FILTER (WHERE state = 'processing') AS processing,"
                    " count(*) FILTER (WHERE state = 'failed') AS failed,"
                    " count(*) FILTER (WHERE state = 'pending' AND attempts > 0) AS retrying,"
                    " coalesce(max(attempts), 0) AS max_attempts"
                    " FROM outbox WHERE state IN ('pending', 'processing', 'failed')"
                )
            )
        ).one()
        failed = (
            await db.execute(
                text(
                    "SELECT event_id, last_error_code, attempts FROM outbox"
                    " WHERE state = 'failed' ORDER BY created_at LIMIT :n"
                ),
                {"n": failed_limit},
            )
        ).all()
    return OutboxStats(
        pending=summary.pending,
        pending_due=summary.due,
        oldest_pending_age_seconds=(
            float(summary.oldest_age) if summary.oldest_age is not None else None
        ),
        processing=summary.processing,
        failed=summary.failed,
        retrying=summary.retrying,
        max_attempts=summary.max_attempts,
        failed_entries=[(r.event_id, r.last_error_code, r.attempts) for r in failed],
    )


async def requeue_failed(
    sessionmaker: async_sessionmaker[AsyncSession], event_id: uuid.UUID
) -> bool:
    """Move ONE failed row back to pending, due now. Attempts, error code and event identity are
    preserved (the composite foreign key still ties it to the same event)."""
    async with sessionmaker() as db, db.begin():
        result = await db.execute(
            text(
                "UPDATE outbox SET state = 'pending', next_attempt_at = now()"
                " WHERE event_id = :id AND state = 'failed'"
            ),
            {"id": event_id},
        )
    return result.rowcount == 1
