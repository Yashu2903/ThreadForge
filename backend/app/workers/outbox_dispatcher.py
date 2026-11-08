"""The standalone outbox dispatcher:  python -m app.workers.outbox_dispatcher

Never started inside an API process. Loop: claim a batch (one short committed transaction), publish
each row's tiny envelope to Redis with NO transaction open, then record the outcome (token-matched).

DELIVERY SEMANTICS, stated plainly:
  * At-least-once publication. If this process publishes and dies before recording completion, the
    lease expires and another worker publishes the same event again. DUPLICATES ARE EXPECTED;
    clients de-duplicate by event_id.
  * A notification can be lost (Redis Pub/Sub keeps nothing; a subscriber may be disconnected), and
    a successful publish does not prove a browser received anything. PostgreSQL is authoritative.
  * No ordering is promised across dispatcher workers; clients order by room_seq.
  * Rows are never silently dropped: transient failures retry forever with backoff; unpublishable
    rows become 'failed' with an actionable code and wait for an operator (outbox_requeue).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import signal
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ConfigError, Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.logging_config import APP_LOGGER, configure_logging
from app.realtime.channels import room_channel
from app.realtime.publisher import Publisher, PublishError, RedisPublisher
from app.schemas.events import OutboxEnvelope
from app.services import outbox

log = logging.getLogger(f"{APP_LOGGER}.outbox")

STATS_INTERVAL_SECONDS = 30.0
PUBLISH_ENVELOPE_MAX_BYTES = 1024


@dataclass
class DispatcherCounters:
    claimed: int = 0
    published: int = 0
    retried: int = 0
    failed: int = 0
    released: int = 0
    stale_claims: int = 0
    zero_subscriber_publishes: int = 0
    last_published_event: str = field(default="-")


class OutboxDispatcher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        publisher: Publisher,
        settings: Settings,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.publisher = publisher
        self.settings = settings
        # Jitter for retry delays: spreading retries out, not a security use of randomness.
        self.rng = rng or random.Random()  # noqa: S311
        self.clock = clock  # injectable so tests can move time without sleeping
        self.counters = DispatcherCounters()
        self.stop_requested = asyncio.Event()

    # ---- one claim cycle -----------------------------------------------------------------------

    async def run_once(self) -> int:
        """Claim one batch and process it. Returns how many rows were claimed (0 = idle)."""
        settings = self.settings
        # The lease starts at the DATABASE's now() inside the claim call, which is no earlier than
        # now, so a deadline measured from BEFORE the call is conservative (never too late).
        started = self.clock()
        batch = await outbox.claim_batch(
            self.sessionmaker,
            batch_size=settings.outbox_batch_size,
            lease_seconds=settings.outbox_claim_lease_seconds,
        )
        if not batch:
            return 0
        self.counters.claimed += len(batch)
        deadline = started + settings.outbox_claim_lease_seconds
        await self._process(batch, deadline)
        return len(batch)

    async def _process(self, batch: list[outbox.ClaimedRow], deadline: float) -> None:
        settings = self.settings
        for index, row in enumerate(batch):
            remaining_rows = batch[index:]
            if self.stop_requested.is_set():
                await self._release(remaining_rows, "shutdown")
                return
            # Deadline rule: if less than one publish timeout of the lease remains, a publish could
            # outlive the lease and another worker would publish the row too. Hand the unprocessed
            # rows back NOW (token-matched) instead of letting the lease expire under us.
            if deadline - self.clock() < settings.outbox_publish_timeout_seconds:
                await self._release(remaining_rows, "lease_deadline")
                return
            await self._publish_one(row)

    async def _publish_one(self, row: outbox.ClaimedRow) -> None:
        # The stored envelope is validated before publishing; it must also agree with the row's
        # own columns (the composite foreign key already ties those to the event).
        try:
            envelope = OutboxEnvelope.model_validate(row.payload)
            if (envelope.event_id, envelope.room_id, envelope.room_seq) != (
                row.event_id,
                row.room_id,
                row.room_seq,
            ):
                raise ValueError("envelope disagrees with the outbox row")
        except (ValidationError, ValueError):
            await self._fail(row, "envelope_invalid")
            return

        message = json.dumps(envelope.model_dump(mode="json"), separators=(",", ":"))
        try:
            subscribers = await self.publisher.publish(
                room_channel(self.settings.redis_channel_prefix, row.room_id), message
            )
        except PublishError as exc:
            await self._retry(row, exc.code)
            return

        if subscribers == 0:
            self.counters.zero_subscriber_publishes += 1  # success: nobody was listening
        if await outbox.mark_processed(self.sessionmaker, row.event_id, row.token):
            self.counters.published += 1
            self.counters.last_published_event = str(row.event_id)
            log.info(
                "outbox_published event_id=%s room_id=%s room_seq=%s attempt=%s subscribers=%s",
                row.event_id,
                row.room_id,
                row.room_seq,
                row.attempts,
                subscribers,
            )
        else:
            # Our lease expired during the publish and the row was re-claimed (or already
            # completed) by someone else. We must not touch it; a duplicate publish is possible.
            self.counters.stale_claims += 1
            log.warning(
                "outbox_stale_claim event_id=%s room_id=%s attempt=%s: completion ignored",
                row.event_id,
                row.room_id,
                row.attempts,
            )

    async def _retry(self, row: outbox.ClaimedRow, code: str) -> None:
        delay = outbox.retry_delay(
            row.attempts, self.settings.outbox_retry_max_delay_seconds, self.rng
        )
        if await outbox.mark_retry(self.sessionmaker, row.event_id, row.token, code, delay):
            self.counters.retried += 1
            log.warning(
                "outbox_retry event_id=%s room_id=%s attempt=%s code=%s retry_in=%.2fs",
                row.event_id,
                row.room_id,
                row.attempts,
                code,
                delay,
            )
        else:
            self.counters.stale_claims += 1
            log.warning(
                "outbox_stale_claim event_id=%s attempt=%s: retry ignored",
                row.event_id,
                row.attempts,
            )

    async def _fail(self, row: outbox.ClaimedRow, code: str) -> None:
        if await outbox.mark_failed(self.sessionmaker, row.event_id, row.token, code):
            self.counters.failed += 1
            log.error(
                "outbox_failed event_id=%s room_id=%s attempt=%s code=%s "
                "(inspect with outbox_status, fix, then outbox_requeue)",
                row.event_id,
                row.room_id,
                row.attempts,
                code,
            )
        else:
            self.counters.stale_claims += 1

    async def _release(self, rows: list[outbox.ClaimedRow], reason: str) -> None:
        released = await outbox.release_claims(
            self.sessionmaker, rows[0].token, [r.event_id for r in rows]
        )
        self.counters.released += released
        log.info("outbox_released count=%s reason=%s", released, reason)

    # ---- the loop ----------------------------------------------------------------------------

    async def run_forever(self) -> None:
        stats_task = asyncio.create_task(self._stats_loop(), name="outbox-stats")
        poll = self.settings.outbox_poll_interval_ms / 1000
        try:
            while not self.stop_requested.is_set():
                try:
                    claimed = await self.run_once()
                except Exception as exc:  # database trouble: rows stay claimed until the lease ends
                    log.error("outbox_cycle_failed error_type=%s", type(exc).__name__)
                    claimed = 0
                if claimed == 0:  # idle (or failing): wait one poll interval, wake early on stop
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.stop_requested.wait(), timeout=poll)
        finally:
            stats_task.cancel()
            await asyncio.gather(stats_task, return_exceptions=True)
            log.info("outbox_dispatcher_stopped %s", self.stats_line())

    def stats_line(self) -> str:
        c = self.counters
        return (
            f"claimed={c.claimed} published={c.published} retried={c.retried} failed={c.failed} "
            f"released={c.released} stale_claims={c.stale_claims}"
        )

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_SECONDS)
            try:
                s = await outbox.outbox_stats(self.sessionmaker, failed_limit=0)
                age = (
                    "-"
                    if s.oldest_pending_age_seconds is None
                    else f"{s.oldest_pending_age_seconds:.1f}s"
                )
                log.info(
                    "outbox_stats pending=%s oldest_pending_age=%s processing=%s failed=%s %s",
                    s.pending,
                    age,
                    s.processing,
                    s.failed,
                    self.stats_line(),
                )
            except Exception as exc:
                log.error("outbox_stats_failed error_type=%s", type(exc).__name__)


async def _amain(settings: Settings) -> None:
    engine = create_engine(settings)
    publisher = RedisPublisher(settings)
    dispatcher = OutboxDispatcher(create_sessionmaker(engine), publisher, settings)
    loop = asyncio.get_running_loop()

    def request_stop(*_args: object) -> None:
        loop.call_soon_threadsafe(dispatcher.stop_requested.set)

    # SIGBREAK (Ctrl+Break) is how a console process is asked to stop on Windows.
    stop_signals = [
        signal.SIGINT,
        signal.SIGTERM,
        *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else []),
    ]
    for sig in stop_signals:
        signal.signal(sig, request_stop)  # add_signal_handler is not available on Windows
    log.info(
        "outbox_dispatcher_started worker=%s batch=%s lease=%ss publish_timeout=%ss",
        uuid.uuid4().hex[:8],
        settings.outbox_batch_size,
        settings.outbox_claim_lease_seconds,
        settings.outbox_publish_timeout_seconds,
    )
    try:
        await dispatcher.run_forever()  # a graceful stop releases unpublished claims
    finally:
        await publisher.aclose()
        await engine.dispose()


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"configuration error:\n{exc}")
        return 2
    configure_logging(settings.log_level)
    asyncio.run(_amain(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
