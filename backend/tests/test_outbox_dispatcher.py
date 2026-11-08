"""The outbox dispatcher: claiming, publishing, retry, leases, token-matched completion.

Lease expiry is tested by editing claimed_until in the database, never by sleeping. Time-dependent
dispatcher logic (the lease deadline rule) uses an injected clock. Fault injection is test-only
(patching functions or using a fake publisher), never a production flag.
"""

import asyncio
import json
import logging
import os
import random
import signal
import subprocess
import sys
import uuid

import pytest

from app.commands.cleanup_auth import cleanup_auth
from app.realtime.channels import room_channel
from app.realtime.publisher import PublishError, RedisPublisher
from app.services import outbox
from app.workers.outbox_dispatcher import OutboxDispatcher
from tests.db_utils import BACKEND_DIR
from tests.factories import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings
from tests.helpers import execute, rows, scalar, seed_outbox
from tests.test_invitations_api import new_room
from tests.test_messages_send import send


class FakePublisher:
    """Records publishes; can be told to fail, to report N subscribers, or to run a hook."""

    def __init__(self, subscribers: int = 1) -> None:
        self.published: list[tuple[str, str]] = []
        self.fail_with: str | None = None
        self.subscribers = subscribers
        self.on_publish = None

    async def publish(self, channel: str, message: str) -> int:
        if self.on_publish is not None:
            await self.on_publish(channel, message)
        if self.fail_with:
            raise PublishError(self.fail_with)
        self.published.append((channel, message))
        return self.subscribers

    async def aclose(self) -> None:
        pass


@pytest.fixture
def dsettings(redis_prefix):
    return make_settings(
        redis_channel_prefix=redis_prefix,
        outbox_claim_lease_seconds=10,
        outbox_publish_timeout_seconds=2,
        outbox_batch_size=50,
    )


@pytest.fixture
def publisher():
    return FakePublisher()


def dispatcher(sessionmaker, publisher, settings, **kwargs):
    return OutboxDispatcher(sessionmaker, publisher, settings, **kwargs)


async def row(sm, event_id):
    [r] = await rows(sm, "SELECT * FROM outbox WHERE event_id = :e", e=event_id)
    return r


async def states(sm):
    return {r["event_id"]: r["state"] for r in await rows(sm, "SELECT event_id, state FROM outbox")}


async def expire_lease(sm, event_id):
    await execute(
        sm,
        "UPDATE outbox SET claimed_until = now() - interval '1 second' WHERE event_id = :e",
        e=event_id,
    )


# ---- publishing ----------------------------------------------------------------------------------------


async def test_a_pending_row_is_published_and_marked_processed(
    alice, sessionmaker, publisher, dsettings
):
    room_id = await new_room(alice)
    [pending] = await rows(sessionmaker, "SELECT * FROM outbox")
    assert pending["state"] == "pending"

    assert await dispatcher(sessionmaker, publisher, dsettings).run_once() == 1

    done = await row(sessionmaker, pending["event_id"])
    assert done["state"] == "processed" and done["processed_at"] is not None
    assert (done["claim_token"], done["claimed_until"], done["attempts"]) == (None, None, 1)
    [(channel, message)] = publisher.published
    assert channel == room_channel(dsettings.redis_channel_prefix, uuid.UUID(room_id))
    assert (
        json.loads(message) == pending["payload"]
    )  # ids only: schema_version, event_id, room_id, room_seq
    assert set(json.loads(message)) == {"schema_version", "event_id", "room_id", "room_seq"}
    assert await dispatcher(sessionmaker, publisher, dsettings).run_once() == 0  # nothing left


async def test_a_real_redis_subscriber_receives_the_notification(
    alice, sessionmaker, dsettings, redis_client
):
    room_id = await new_room(alice)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(room_channel(dsettings.redis_channel_prefix, uuid.UUID(room_id)))
    assert (await pubsub.get_message(timeout=2))["type"] == "subscribe"

    real = RedisPublisher(dsettings)
    try:
        await dispatcher(sessionmaker, real, dsettings).run_once()
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=3)
    finally:
        await real.aclose()
        await pubsub.aclose()
    assert message is not None
    [pending] = await rows(sessionmaker, "SELECT * FROM outbox")
    assert json.loads(message["data"]) == pending["payload"] and pending["state"] == "processed"


async def test_zero_subscribers_still_counts_as_success(alice, sessionmaker, dsettings):
    await new_room(alice)
    quiet = FakePublisher(subscribers=0)
    d = dispatcher(sessionmaker, quiet, dsettings)
    await d.run_once()
    assert list((await states(sessionmaker)).values()) == ["processed"]
    assert d.counters.zero_subscriber_publishes == 1 and d.counters.published == 1


async def test_a_real_publish_with_nobody_subscribed_is_success(alice, sessionmaker, dsettings):
    await new_room(alice)
    real = RedisPublisher(dsettings)
    try:
        await dispatcher(sessionmaker, real, dsettings).run_once()
    finally:
        await real.aclose()
    assert list((await states(sessionmaker)).values()) == ["processed"]


# ---- failure and retry ----------------------------------------------------------------------------------


async def test_a_redis_failure_leaves_the_row_pending_with_a_scheduled_retry(
    alice, sessionmaker, publisher, dsettings
):
    await new_room(alice)
    publisher.fail_with = "redis_timeout"
    d = dispatcher(sessionmaker, publisher, dsettings)
    await d.run_once()

    [r] = await rows(sessionmaker, "SELECT *, next_attempt_at > now() AS in_future FROM outbox")
    assert r["state"] == "pending" and r["attempts"] == 1
    assert (r["claim_token"], r["claimed_until"], r["last_error_code"]) == (
        None,
        None,
        "redis_timeout",
    )
    assert r["in_future"] is True  # a retry is never immediate
    assert d.counters.retried == 1
    assert await d.run_once() == 0  # not due yet: nothing is claimed again


async def test_an_unreachable_redis_gives_a_short_safe_error_code(
    alice, sessionmaker, redis_prefix
):
    await new_room(alice)
    settings = make_settings(
        redis_url="redis://127.0.0.1:1/15",  # nothing listens here
        redis_channel_prefix=redis_prefix,
        outbox_publish_timeout_seconds=2,
    )
    real = RedisPublisher(settings)
    try:
        await dispatcher(sessionmaker, real, settings).run_once()
    finally:
        await real.aclose()
    [r] = await rows(sessionmaker, "SELECT * FROM outbox")
    # Both are bounded, safe codes. Which one occurs depends on the platform: Linux refuses a closed
    # port at once (redis_connection); on Windows the attempt can outlast the 2 s publish timeout.
    assert r["state"] == "pending" and r["last_error_code"] in {"redis_connection", "redis_timeout"}
    assert "127.0.0.1" not in str(r["last_error_code"])  # no URL, host or credential in the code


async def test_a_retried_row_is_published_once_it_is_due_again(
    alice, sessionmaker, publisher, dsettings
):
    await new_room(alice)
    publisher.fail_with = "redis_connection"
    d = dispatcher(sessionmaker, publisher, dsettings)
    await d.run_once()
    publisher.fail_with = None  # Redis is back
    await execute(sessionmaker, "UPDATE outbox SET next_attempt_at = now() - interval '1 second'")
    assert await d.run_once() == 1
    [r] = await rows(sessionmaker, "SELECT * FROM outbox")
    assert (r["state"], r["attempts"], r["last_error_code"]) == ("processed", 2, "redis_connection")
    assert len(publisher.published) == 1


async def test_backoff_is_bounded_exponential_with_equal_jitter_and_never_immediate():
    rng = random.Random(1234)
    max_delay = 60.0
    seen: set[float] = set()
    for attempts in range(1, 40):
        cap = min(max_delay, 0.5 * 2 ** (attempts - 1))
        samples = [outbox.retry_delay(attempts, max_delay, rng) for _ in range(300)]
        assert all(cap / 2 <= d <= cap for d in samples), attempts  # half fixed, half random
        assert min(samples) >= 0.25 > 0  # never immediate
        assert max(samples) <= max_delay  # never above the configured cap
        assert len(set(samples)) > 100  # genuinely jittered, not constant
        seen.update(samples)
    assert [round(0.5 * 2 ** (a - 1), 3) for a in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 4.0]
    assert outbox.retry_delay(10_000, 60.0, rng) <= 60.0  # huge attempt counts cannot overflow
    assert outbox.retry_delay(0, 60.0, rng) <= 0.5  # defensive: attempts below 1 behave as 1


async def test_an_invalid_envelope_fails_permanently_and_is_never_published(
    alice, sessionmaker, publisher, dsettings
):
    await new_room(alice)
    await execute(sessionmaker, "UPDATE outbox SET payload = '{\"schema_version\": 1}'")
    d = dispatcher(sessionmaker, publisher, dsettings)
    await d.run_once()
    [r] = await rows(sessionmaker, "SELECT * FROM outbox")
    assert (r["state"], r["last_error_code"]) == ("failed", "envelope_invalid")
    assert (r["claim_token"], r["claimed_until"]) == (None, None)
    assert publisher.published == [] and d.counters.failed == 1
    assert await d.run_once() == 0  # a failed row is not retried automatically


async def test_an_envelope_that_disagrees_with_its_row_fails(
    alice, sessionmaker, publisher, dsettings
):
    await new_room(alice)
    other = uuid.uuid4()
    await execute(
        sessionmaker,
        "UPDATE outbox SET payload = jsonb_set(payload, '{event_id}', to_jsonb(CAST(:o AS text)))",
        o=str(other),
    )
    await dispatcher(sessionmaker, publisher, dsettings).run_once()
    assert (await rows(sessionmaker, "SELECT last_error_code FROM outbox"))[0][
        "last_error_code"
    ] == "envelope_invalid"
    assert publisher.published == []


async def test_a_failed_row_is_requeued_with_identity_and_attempts_preserved(
    alice, sessionmaker, publisher, dsettings
):
    await new_room(alice)
    [before] = await rows(sessionmaker, "SELECT * FROM outbox")
    await execute(sessionmaker, "UPDATE outbox SET payload = '{}'")
    await dispatcher(sessionmaker, publisher, dsettings).run_once()
    assert (await row(sessionmaker, before["event_id"]))["state"] == "failed"

    assert await outbox.requeue_failed(sessionmaker, before["event_id"]) is True
    [after] = await rows(sessionmaker, "SELECT *, next_attempt_at <= now() AS due FROM outbox")
    assert (after["state"], after["attempts"], after["event_id"]) == (
        "pending",
        1,
        before["event_id"],
    )
    assert (after["room_id"], after["room_seq"]) == (before["room_id"], before["room_seq"])
    assert (
        after["due"] is True and after["last_error_code"] == "envelope_invalid"
    )  # kept for context
    assert (
        await outbox.requeue_failed(sessionmaker, before["event_id"]) is False
    )  # not failed anymore
    assert await outbox.requeue_failed(sessionmaker, uuid.uuid4()) is False  # unknown id
    # fix the envelope and the row publishes normally
    await execute(sessionmaker, "UPDATE outbox SET payload = :p", p=json.dumps(before["payload"]))
    await dispatcher(sessionmaker, publisher, dsettings).run_once()
    assert (await row(sessionmaker, before["event_id"]))["state"] == "processed"


# ---- claiming and leases ------------------------------------------------------------------------------


async def test_only_due_rows_are_claimed_up_to_the_batch_size(alice, sessionmaker):
    _, ids = await seed_outbox(sessionmaker, alice.id, 12)
    await execute(
        sessionmaker,
        "UPDATE outbox SET next_attempt_at = now() + interval '1 hour' WHERE event_id = :e",
        e=ids[0],
    )
    claimed = await outbox.claim_batch(sessionmaker, batch_size=5, lease_seconds=10)
    assert len(claimed) == 5 and ids[0] not in {c.event_id for c in claimed}
    assert len({c.token for c in claimed}) == 1  # ONE fresh claim token per claim call
    assert all(c.attempts == 1 for c in claimed)
    rest = await outbox.claim_batch(sessionmaker, batch_size=100, lease_seconds=10)
    assert len(rest) == 6 and {c.event_id for c in claimed}.isdisjoint(c.event_id for c in rest)
    assert rest[0].token != claimed[0].token  # each claim call gets its own token
    claimed_rows = await rows(sessionmaker, "SELECT * FROM outbox WHERE state = 'processing'")
    assert len(claimed_rows) == 11 and all(
        r["claim_token"] and r["claimed_until"] for r in claimed_rows
    )


async def test_unexpired_claims_are_not_reclaimed_but_expired_ones_are(alice, sessionmaker):
    _, ids = await seed_outbox(sessionmaker, alice.id, 3)
    first = await outbox.claim_batch(sessionmaker, batch_size=10, lease_seconds=30)
    assert len(first) == 3
    assert (
        await outbox.claim_batch(sessionmaker, batch_size=10, lease_seconds=30) == []
    )  # leases live
    await expire_lease(sessionmaker, ids[1])  # edit claimed_until, no sleeping
    again = await outbox.claim_batch(sessionmaker, batch_size=10, lease_seconds=30)
    assert [c.event_id for c in again] == [ids[1]]
    assert again[0].attempts == 2 and again[0].token != first[0].token  # a NEW token and attempt


async def test_an_expired_claim_is_reclaimed_and_published_by_a_dispatcher(
    alice, sessionmaker, publisher, dsettings
):
    _, [event_id] = await seed_outbox(sessionmaker, alice.id, 1)
    abandoned = await outbox.claim_batch(
        sessionmaker, batch_size=10, lease_seconds=30
    )  # a dead worker
    assert abandoned and await dispatcher(sessionmaker, publisher, dsettings).run_once() == 0
    await expire_lease(sessionmaker, event_id)
    assert await dispatcher(sessionmaker, publisher, dsettings).run_once() == 1
    done = await row(sessionmaker, event_id)
    assert (done["state"], done["attempts"]) == ("processed", 2)
    assert len(publisher.published) == 1


@pytest.mark.parametrize("round_", range(3))
async def test_two_concurrent_claim_calls_never_share_a_row(alice, sessionmaker, round_):
    _, ids = await seed_outbox(sessionmaker, alice.id, 40)
    first, second, third = await asyncio.gather(
        outbox.claim_batch(sessionmaker, batch_size=18, lease_seconds=10),
        outbox.claim_batch(sessionmaker, batch_size=18, lease_seconds=10),
        outbox.claim_batch(sessionmaker, batch_size=18, lease_seconds=10),
    )
    sets = [{c.event_id for c in batch} for batch in (first, second, third)]
    assert sum(len(s) for s in sets) == len(set().union(*sets))  # pairwise disjoint
    assert set().union(*sets) == set(ids)  # and nothing was left unclaimed (40 <= 3 * 18)
    assert len({batch[0].token for batch in (first, second, third) if batch}) == len(
        [b for b in (first, second, third) if b]
    )


async def test_a_stale_claim_token_cannot_complete_fail_retry_or_release(alice, sessionmaker):
    _, [event_id] = await seed_outbox(sessionmaker, alice.id, 1)
    [old] = await outbox.claim_batch(sessionmaker, batch_size=1, lease_seconds=30)  # worker A
    await expire_lease(sessionmaker, event_id)
    [new] = await outbox.claim_batch(sessionmaker, batch_size=1, lease_seconds=30)  # worker B
    assert old.token != new.token
    before = await row(sessionmaker, event_id)

    assert await outbox.mark_processed(sessionmaker, event_id, old.token) is False
    assert await outbox.mark_failed(sessionmaker, event_id, old.token, "x") is False
    assert await outbox.mark_retry(sessionmaker, event_id, old.token, "x", 5.0) is False
    assert await outbox.release_claims(sessionmaker, old.token, [event_id]) == 0
    assert await row(sessionmaker, event_id) == before  # B's claim is untouched

    # a completed row cannot be overwritten by a stale worker either
    assert await outbox.mark_processed(sessionmaker, event_id, new.token) is True
    done = await row(sessionmaker, event_id)
    assert await outbox.mark_retry(sessionmaker, event_id, new.token, "x", 5.0) is False
    assert await outbox.mark_processed(sessionmaker, event_id, new.token) is False
    assert await row(sessionmaker, event_id) == done


async def test_a_dispatcher_whose_lease_expired_mid_publish_ignores_its_completion(
    alice, sessionmaker, publisher, dsettings, caplog
):
    caplog.set_level(logging.INFO, logger="threadforge")
    _, [event_id] = await seed_outbox(sessionmaker, alice.id, 1)
    stolen = {}

    async def another_worker_takes_over(_channel, _message):
        await expire_lease(sessionmaker, event_id)  # our lease runs out during the publish...
        [stolen["claim"]] = await outbox.claim_batch(sessionmaker, batch_size=1, lease_seconds=30)

    publisher.on_publish = another_worker_takes_over
    d = dispatcher(sessionmaker, publisher, dsettings)
    await d.run_once()
    assert d.counters.stale_claims == 1 and d.counters.published == 0
    assert "outbox_stale_claim" in caplog.text and str(event_id) in caplog.text
    current = await row(sessionmaker, event_id)
    assert (current["state"], current["claim_token"]) == ("processing", stolen["claim"].token)


async def test_a_crash_after_publish_leads_to_a_duplicate_publish(
    alice, sessionmaker, publisher, dsettings, monkeypatch
):
    """Publish succeeds, the worker dies before recording completion, the lease expires, another
    worker publishes again. Duplicates are expected; that is why clients de-duplicate by event_id."""
    _, [event_id] = await seed_outbox(sessionmaker, alice.id, 1)

    class Crash(BaseException):
        pass

    async def die(*_args, **_kwargs):
        raise Crash

    monkeypatch.setattr(outbox, "mark_processed", die)
    with pytest.raises(Crash):
        await dispatcher(sessionmaker, publisher, dsettings).run_once()
    assert len(publisher.published) == 1  # it WAS published
    assert (await row(sessionmaker, event_id))["state"] == "processing"  # but never recorded

    monkeypatch.undo()
    await expire_lease(sessionmaker, event_id)
    await dispatcher(sessionmaker, publisher, dsettings).run_once()
    assert len(publisher.published) == 2 and publisher.published[0] == publisher.published[1]
    assert (await row(sessionmaker, event_id))["state"] == "processed"


async def test_the_lease_deadline_rule_releases_unprocessed_rows_instead_of_letting_them_expire(
    alice, sessionmaker, publisher, dsettings
):
    _, ids = await seed_outbox(sessionmaker, alice.id, 3)
    now = [0.0]
    d = dispatcher(sessionmaker, publisher, dsettings, clock=lambda: now[0])  # lease 10, timeout 2

    async def slow_publish(_c, _m):
        now[0] += 9.0  # the first publish "takes" 9 s: only 1 s of lease remains (< 2 s timeout)

    publisher.on_publish = slow_publish
    await d.run_once()

    assert len(publisher.published) == 1  # nothing else was published past the safe point
    after = await states(sessionmaker)
    assert [after[i] for i in ids] == ["processed", "pending", "pending"]
    released = [await row(sessionmaker, i) for i in ids[1:]]
    assert all(
        r["claim_token"] is None and r["claimed_until"] is None for r in released
    )  # NOW free
    assert d.counters.released == 2
    # and they are immediately claimable by anyone: no waiting for the lease to run out
    assert len(await outbox.claim_batch(sessionmaker, batch_size=10, lease_seconds=10)) == 2


async def test_graceful_shutdown_finishes_the_inflight_publish_and_releases_the_rest(
    alice, sessionmaker, publisher, dsettings
):
    _, ids = await seed_outbox(sessionmaker, alice.id, 4)
    d = dispatcher(sessionmaker, publisher, dsettings)

    async def stop_during_first_publish(_c, _m):
        d.stop_requested.set()  # SIGTERM arrives while the first publish is in flight

    publisher.on_publish = stop_during_first_publish
    await d.run_once()
    after = await states(sessionmaker)
    assert [after[i] for i in ids] == [
        "processed",
        "pending",
        "pending",
        "pending",
    ]  # in-flight recorded
    for event_id in ids[1:]:
        assert (await row(sessionmaker, event_id))["claim_token"] is None  # claims released
    assert len(publisher.published) == 1 and d.counters.released == 3


async def test_run_forever_publishes_new_rows_and_stops_promptly(alice, sessionmaker, publisher):
    settings = make_settings(outbox_poll_interval_ms=20)
    d = dispatcher(sessionmaker, publisher, settings)
    task = asyncio.create_task(d.run_forever())
    try:
        _, ids = await seed_outbox(sessionmaker, alice.id, 3)
        for _ in range(100):
            if all(s == "processed" for s in (await states(sessionmaker)).values()):
                break
            await asyncio.sleep(0.05)
        assert all(s == "processed" for s in (await states(sessionmaker)).values())
        assert len(publisher.published) == 3 and len(ids) == 3
    finally:
        d.stop_requested.set()
        await asyncio.wait_for(task, timeout=5)


async def test_dispatcher_logs_carry_ids_and_codes_but_never_payload_or_message_text(
    alice, sessionmaker, publisher, dsettings, caplog
):
    caplog.set_level(logging.DEBUG)
    room_id = await new_room(alice)
    await send(alice, room_id, "SECRET-TEXT-IN-A-MESSAGE-3391")
    d = dispatcher(sessionmaker, publisher, dsettings)
    publisher.fail_with = "redis_timeout"
    await d.run_once()
    publisher.fail_with = None
    await execute(sessionmaker, "UPDATE outbox SET next_attempt_at = now() - interval '1 second'")
    await d.run_once()
    text = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("threadforge.outbox")
    )
    assert "outbox_retry" in text and "outbox_published" in text
    assert "code=redis_timeout" in text and "attempt=" in text and f"room_id={room_id}" in text
    assert "SECRET-TEXT" not in text and "schema_version" not in text  # no payloads in logs


# ---- operator commands and cleanup ----------------------------------------------------------------------


def _env(redis_prefix):
    return {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "CSRF_SECRET": "test-csrf-secret-0123456789-abcdefghijklmnop",
        "MESSAGE_FINGERPRINT_SECRET": "test-fingerprint-secret-0123456789-abcdefghij",
        "REDIS_URL": TEST_REDIS_URL,
        "REDIS_CHANNEL_PREFIX": redis_prefix,
        "OUTBOX_POLL_INTERVAL_MS": "50",
    }


def _run_cli(module, redis_prefix, *args):
    return subprocess.run(  # noqa: S603 - fixed argv, test-only
        [sys.executable, "-m", module, *args],
        cwd=BACKEND_DIR,
        env=_env(redis_prefix),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


async def test_the_status_and_requeue_commands(
    alice, sessionmaker, publisher, dsettings, redis_prefix
):
    _, ids = await seed_outbox(sessionmaker, alice.id, 4)
    await execute(
        sessionmaker, "UPDATE outbox SET payload = '{}' WHERE event_id = :e", e=ids[0]
    )  # will fail
    await execute(
        sessionmaker,
        "UPDATE outbox SET next_attempt_at = now() - interval '1 hour' WHERE event_id = :e",
        e=ids[0],
    )  # claimed first
    await execute(
        sessionmaker,
        "UPDATE outbox SET attempts = 3, last_error_code = 'redis_timeout' WHERE event_id = :e",
        e=ids[1],
    )
    await dispatcher(
        sessionmaker, publisher, make_settings(outbox_batch_size=1)
    ).run_once()  # claims ids[0] only
    assert (await row(sessionmaker, ids[0]))["state"] == "failed"

    status = await asyncio.to_thread(_run_cli, "app.commands.outbox_status", redis_prefix)
    assert status.returncode == 0, status.stderr
    out = status.stdout
    assert "pending:" in out and "failed:                  1" in out
    assert f"event_id={ids[0]}" in out and "error=envelope_invalid" in out
    assert "oldest pending age" in out and "highest attempt count" in out
    assert "room_seq" not in out and "schema_version" not in out  # no payloads

    requeue = await asyncio.to_thread(
        _run_cli, "app.commands.outbox_requeue", redis_prefix, "--event-id", str(ids[0])
    )
    assert requeue.returncode == 0 and "requeued" in requeue.stdout
    assert (await row(sessionmaker, ids[0]))["state"] == "pending"
    again = await asyncio.to_thread(
        _run_cli, "app.commands.outbox_requeue", redis_prefix, "--event-id", str(ids[0])
    )
    assert again.returncode == 1 and "nothing requeued" in again.stderr
    bad = await asyncio.to_thread(
        _run_cli, "app.commands.outbox_requeue", redis_prefix, "--event-id", "not-a-uuid"
    )
    assert bad.returncode != 0


async def test_cleanup_deletes_only_old_processed_outbox_rows(alice, sessionmaker):
    _, ids = await seed_outbox(sessionmaker, alice.id, 8)
    # 0: old processed (deleted)  1: old processed (deleted)  2: recent processed (kept)
    # 3: old but FAILED (kept)  4: old pending (kept)  5: old processing (kept)  6,7: untouched pending
    await execute(
        sessionmaker,
        "UPDATE outbox SET state='processed', processed_at = now() - interval '30 days' WHERE event_id = ANY(CAST(:ids AS uuid[]))",
        ids=[ids[0], ids[1]],
    )
    await execute(
        sessionmaker,
        "UPDATE outbox SET state='processed', processed_at = now() - interval '1 hour' WHERE event_id = :e",
        e=ids[2],
    )
    await execute(
        sessionmaker,
        "UPDATE outbox SET state='failed', last_error_code='envelope_invalid', created_at = now() - interval '40 days' WHERE event_id = :e",
        e=ids[3],
    )
    await execute(
        sessionmaker,
        "UPDATE outbox SET created_at = now() - interval '40 days' WHERE event_id = :e",
        e=ids[4],
    )
    await execute(
        sessionmaker,
        "UPDATE outbox SET state='processing', claim_token = gen_random_uuid(), claimed_until = now() - interval '1 day' WHERE event_id = :e",
        e=ids[5],
    )
    events_before = await scalar(sessionmaker, "SELECT count(*) FROM room_events")

    result = await cleanup_auth(sessionmaker, batch_size=1, outbox_retention_days=7)
    assert result.outbox_processed == 2
    assert set(await states(sessionmaker)) == set(
        ids[2:]
    )  # only the two old processed rows are gone
    assert (await row(sessionmaker, ids[3]))["state"] == "failed"
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM room_events") == events_before
    )  # events stay
    assert (await cleanup_auth(sessionmaker, outbox_retention_days=7)).outbox_processed == 0
    assert (
        await cleanup_auth(sessionmaker, outbox_retention_days=0)
    ).outbox_processed == 1  # ids[2]


async def test_the_cleanup_command_reads_the_retention_setting(alice, sessionmaker, redis_prefix):
    _, ids = await seed_outbox(sessionmaker, alice.id, 2)
    await execute(
        sessionmaker,
        "UPDATE outbox SET state='processed', processed_at = now() - interval '3 days'",
    )
    env = {**_env(redis_prefix), "OUTBOX_PROCESSED_RETENTION_DAYS": "2"}
    done = await asyncio.to_thread(
        lambda: subprocess.run(  # noqa: S603
            [sys.executable, "-m", "app.commands.cleanup_auth"],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    )
    assert done.returncode == 0 and "deleted 2 processed outbox rows" in done.stdout, done.stderr
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox") == 0 and len(ids) == 2


# ---- a real dispatcher process --------------------------------------------------------------------------


async def test_a_real_dispatcher_subprocess_publishes_and_stops_gracefully(
    alice, sessionmaker, redis_prefix, redis_client
):
    pubsub = redis_client.pubsub()
    room_id = await new_room(alice)  # the pending room_created row exists BEFORE the process starts
    await pubsub.subscribe(room_channel(redis_prefix, uuid.UUID(room_id)))
    assert (await pubsub.get_message(timeout=2))["type"] == "subscribe"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(  # noqa: S603, ASYNC220 - fixed argv, test-only
        [sys.executable, "-m", "app.workers.outbox_dispatcher"],
        cwd=BACKEND_DIR,
        env=_env(redis_prefix),
        creationflags=flags,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        message = None
        for _ in range(100):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message:
                break
        assert message is not None, "the dispatcher process never published"
        [pending] = await rows(sessionmaker, "SELECT * FROM outbox")
        assert json.loads(message["data"]) == pending["payload"]
        for _ in range(40):
            if (await row(sessionmaker, pending["event_id"]))["state"] == "processed":
                break
            await asyncio.sleep(0.1)
        assert (await row(sessionmaker, pending["event_id"]))["state"] == "processed"
    finally:
        await pubsub.aclose()
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        try:
            output, _ = await asyncio.to_thread(proc.communicate, None, 15)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
            pytest.fail("the dispatcher did not stop gracefully within 15 s")
    assert "outbox_dispatcher_stopped" in output and "outbox_published" in output
