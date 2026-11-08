"""The subscription manager against a real Redis: acknowledgements, reference counts, races,
validation of incoming notifications, queue overflow, outage and reconnect.

Each test uses its own unique channel prefix (see conftest), on the dedicated test database index.
"""

import asyncio
import json
import logging
import random
import uuid

import pytest
import pytest_asyncio

from app.realtime.channels import room_channel, room_id_from_channel
from app.realtime.notifications import (
    MAX_NOTIFICATION_BYTES,
    NotificationRejected,
    parse_notification,
)
from app.realtime.publisher import create_redis_client
from app.realtime.subscriptions import SubscribeFailed, SubscriptionManager
from tests.factories import TEST_REDIS_URL, make_settings


def envelope(room_id, seq=1, event_id=None, **overrides):
    body = {
        "schema_version": 1,
        "event_id": str(event_id or uuid.uuid4()),
        "room_id": str(room_id),
        "room_seq": seq,
    }
    body.update(overrides)
    return json.dumps(body)


@pytest.fixture
def small_settings(redis_prefix):
    return make_settings(
        redis_channel_prefix=redis_prefix, realtime_queue_size=3, ws_subscribe_timeout_seconds=2
    )


@pytest_asyncio.fixture
async def manager(settings, redis_client):
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.realtime_queue_size)
    mgr = SubscriptionManager(settings, queue)
    mgr.queue = queue  # convenience for the tests
    await mgr.start()
    yield mgr
    await mgr.stop()


async def numsub(redis_client, prefix, room_id):
    [(_, count)] = await redis_client.pubsub_numsub(room_channel(prefix, room_id))
    return count


async def wait_for(predicate, within=5.0):
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


# ---- pure parsing -----------------------------------------------------------------------------------------


def test_channel_names_round_trip_and_reject_foreign_channels():
    room = uuid.uuid4()
    channel = room_channel("threadforge", room)
    assert channel == f"threadforge:room:{room}"
    assert room_id_from_channel("threadforge", channel) == room
    for bad in (
        f"other:room:{room}",
        f"threadforge:room:{str(room).upper()}",
        "threadforge:room:x",
        "threadforge:room:",
    ):
        assert room_id_from_channel("threadforge", bad) is None


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ("x" * (MAX_NOTIFICATION_BYTES + 1), "oversized"),
        ("not json at all", "not_json"),
        ("[]", "schema"),
        ('{"schema_version": 1}', "schema"),
        (
            json.dumps(
                {"schema_version": 2, "event_id": str(uuid.uuid4()), "room_id": "r", "room_seq": 1}
            ),
            "schema",
        ),
    ],
)
def test_parse_notification_rejects_bad_input(data, reason):
    room = uuid.uuid4()
    with pytest.raises(NotificationRejected) as caught:
        parse_notification("p", room_channel("p", room), data)
    assert caught.value.reason == reason


def test_parse_notification_rejects_extra_fields_bad_seq_and_channel_mismatch():
    room, other = uuid.uuid4(), uuid.uuid4()
    channel = room_channel("p", room)
    for bad, reason in (
        (envelope(room, extra="field"), "schema"),
        (envelope(room, seq=0), "schema"),
        (envelope(room, seq="3"), "schema"),
        (envelope(other), "channel_mismatch"),  # room B's event on room A's channel
    ):
        with pytest.raises(NotificationRejected) as caught:
            parse_notification("p", channel, bad)
        assert caught.value.reason == reason
    with pytest.raises(NotificationRejected, match="bad_channel"):
        parse_notification("p", "p:something:else", envelope(room))
    good = parse_notification("p", channel, envelope(room, seq=7).encode())  # bytes are fine
    assert (good.room_id, good.room_seq) == (room, 7)


# ---- acknowledgements and reference counts -------------------------------------------------------------------


async def test_acquire_returns_only_after_redis_acknowledged_the_subscription(
    manager, redis_client, redis_prefix
):
    room = uuid.uuid4()
    await manager.acquire(room)
    # The instant acquire returns, the SERVER already counts the subscription: ack, not "sent".
    assert await numsub(redis_client, redis_prefix, room) == 1
    assert room_channel(redis_prefix, room) in manager.subscribed_channels
    await redis_client.publish(room_channel(redis_prefix, room), envelope(room, seq=5))
    notification = await asyncio.wait_for(manager.queue.get(), timeout=3)
    assert (notification.room_id, notification.room_seq) == (room, 5)


async def test_reference_counting_unsubscribes_only_when_the_last_connection_leaves(
    manager, redis_client, redis_prefix
):
    room = uuid.uuid4()
    await manager.acquire(room)
    await manager.acquire(room)
    assert (
        manager.reference_counts == {room: 2}
        and await numsub(redis_client, redis_prefix, room) == 1
    )
    manager.release_nowait(room)
    await manager.wait_idle()
    assert (
        manager.reference_counts == {room: 1}
        and await numsub(redis_client, redis_prefix, room) == 1
    )
    manager.release_nowait(room)
    await manager.wait_idle()
    assert manager.reference_counts == {} and manager.subscribed_channels == frozenset()
    assert await numsub(redis_client, redis_prefix, room) == 0


async def test_many_connections_arriving_at_once_share_one_subscription(
    manager, redis_client, redis_prefix
):
    room = uuid.uuid4()
    await asyncio.gather(*[manager.acquire(room) for _ in range(12)])
    assert manager.reference_counts == {room: 12}
    assert (
        len(manager.subscribed_channels) == 1
        and await numsub(redis_client, redis_prefix, room) == 1
    )


async def test_a_connection_arriving_during_unsubscribe_waits_and_resubscribes(
    manager, redis_client, redis_prefix
):
    room = uuid.uuid4()
    await manager.acquire(room)
    manager.release_nowait(room)  # the last connection leaves: UNSUBSCRIBE is now queued...
    await manager.acquire(
        room
    )  # ...and a new one arrives immediately: it waits, then re-subscribes
    assert manager.reference_counts == {room: 1}
    assert room_channel(redis_prefix, room) in manager.subscribed_channels
    assert await numsub(redis_client, redis_prefix, room) == 1
    await redis_client.publish(room_channel(redis_prefix, room), envelope(room, seq=1))
    assert (await asyncio.wait_for(manager.queue.get(), timeout=3)).room_seq == 1  # really live


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_rapid_random_acquire_and_release_leaves_the_correct_state_and_no_leaks(
    manager, redis_client, redis_prefix, seed
):
    rng = random.Random(seed)
    rooms = [uuid.uuid4() for _ in range(5)]

    async def client_connection():
        room = rng.choice(rooms)
        await asyncio.sleep(rng.random() * 0.05)
        await manager.acquire(room)
        assert (
            room_channel(redis_prefix, room) in manager.subscribed_channels
        )  # never "ready" early
        await asyncio.sleep(rng.random() * 0.05)
        manager.release_nowait(room)

    await asyncio.gather(*[client_connection() for _ in range(60)])
    await manager.wait_idle()
    assert manager.reference_counts == {} and manager.subscribed_channels == frozenset()
    assert await redis_client.pubsub_channels(f"{redis_prefix}:*") == []  # nothing leaked in Redis
    assert (
        len([t for t in asyncio.all_tasks() if t.get_name().startswith("redis-")]) == 2
    )  # no stray tasks


async def test_a_cancelled_acquire_never_leaks_a_reference(settings, redis_client, redis_prefix):
    """Redis unreachable: acquires hang. Cancelling them must not leave references behind."""
    state = {"url": "redis://127.0.0.1:1/15"}
    settings = make_settings(redis_channel_prefix=redis_prefix, ws_subscribe_timeout_seconds=1)
    queue: asyncio.Queue = asyncio.Queue()
    mgr = SubscriptionManager(
        settings,
        queue,
        client_factory=lambda: create_redis_client(make_settings(redis_url=state["url"])),
    )
    await mgr.start()
    try:
        room = uuid.uuid4()
        tasks = [asyncio.create_task(mgr.acquire(room)) for _ in range(4)]
        await asyncio.sleep(0.2)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(mgr.wait_idle(), timeout=10)
        assert mgr.reference_counts == {}
        state["url"] = TEST_REDIS_URL  # Redis "comes back" (the reader reconnects with backoff)
        await wait_for(lambda: mgr.status()["connected"], within=30)
        await mgr.acquire(room)  # a fresh acquire now works
        assert mgr.reference_counts == {room: 1}
    finally:
        await mgr.stop()


# ---- validation of what Redis delivers ---------------------------------------------------------------------------


async def test_malformed_oversized_and_mismatched_notifications_are_dropped_and_counted(
    manager, redis_client, redis_prefix
):
    room, other = uuid.uuid4(), uuid.uuid4()
    channel = room_channel(redis_prefix, room)
    await manager.acquire(room)
    hostile = [
        "x" * 2000,  # oversized
        "garbage",  # not JSON
        "[1, 2, 3]",  # wrong shape
        envelope(room, extra="field"),  # extra field
        envelope(room, seq=-4),  # invalid sequence
        envelope(other),  # event for ANOTHER room on this room's channel
    ]
    for payload in hostile:
        await redis_client.publish(channel, payload)
    await redis_client.publish(channel, envelope(room, seq=99))  # one valid message afterwards
    delivered = await asyncio.wait_for(manager.queue.get(), timeout=3)
    assert delivered.room_seq == 99 and manager.queue.empty()  # only the valid one got through
    assert manager.rejected == {"oversized": 1, "not_json": 1, "schema": 3, "channel_mismatch": 1}
    assert manager.notifications_received == 7


async def test_queue_overflow_drops_the_oldest_and_counts(
    small_settings, redis_client, redis_prefix
):
    queue: asyncio.Queue = asyncio.Queue(maxsize=3)  # nothing consumes it
    mgr = SubscriptionManager(small_settings, queue)
    await mgr.start()
    try:
        room = uuid.uuid4()
        await mgr.acquire(room)
        for seq in range(1, 7):
            await redis_client.publish(room_channel(redis_prefix, room), envelope(room, seq=seq))
        await wait_for(lambda: mgr.notifications_received == 6)
        kept = [queue.get_nowait().room_seq for _ in range(queue.qsize())]
        assert kept == [4, 5, 6]  # the newest three survive; the OLDEST were dropped
        assert mgr.dropped_notifications == 3 and mgr.status()["dropped_notifications"] == 3
    finally:
        await mgr.stop()


# ---- outage and reconnect ----------------------------------------------------------------------------------------


async def test_acquire_fails_in_bounded_time_when_redis_is_unreachable_then_recovers(
    redis_prefix, caplog
):
    caplog.set_level(logging.INFO, logger="threadforge")
    state = {"url": "redis://127.0.0.1:1/15"}
    settings = make_settings(redis_channel_prefix=redis_prefix, ws_subscribe_timeout_seconds=1)
    queue: asyncio.Queue = asyncio.Queue()
    mgr = SubscriptionManager(
        settings,
        queue,
        client_factory=lambda: create_redis_client(make_settings(redis_url=state["url"])),
    )
    await mgr.start()  # starting must never need Redis
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(SubscribeFailed):
            await mgr.acquire(uuid.uuid4())
        assert asyncio.get_running_loop().time() - started < 3  # bounded, not hanging
        await asyncio.wait_for(mgr.wait_idle(), timeout=10)
        assert mgr.reference_counts == {}  # a failed acquire holds no reference
        # A connect attempt to a closed port is slow on Windows (longer than the 1 s subscribe
        # timeout), so wait for the failed attempt itself to be recorded.
        await wait_for(lambda: mgr.connect_failures >= 1, within=20)
        assert "redis_pubsub_connect_failed" in caplog.text

        state["url"] = TEST_REDIS_URL  # Redis returns
        await wait_for(lambda: mgr.status()["connected"], within=30)
        room = uuid.uuid4()
        await mgr.acquire(room)
        assert room_channel(redis_prefix, room) in mgr.subscribed_channels
    finally:
        await mgr.stop()


async def test_a_dropped_connection_reconnects_resubscribes_and_flags_a_possible_gap(
    manager, redis_client, redis_prefix, caplog
):
    caplog.set_level(logging.INFO, logger="threadforge")
    rooms = [uuid.uuid4(), uuid.uuid4()]
    for room in rooms:
        await manager.acquire(room)
    await manager.acquire(rooms[0])  # two connections on the first room
    assert manager.connection_losses == 0

    await redis_client.execute_command("CLIENT", "KILL", "TYPE", "pubsub")  # sever every subscriber
    await wait_for(lambda: manager.connection_losses >= 1, within=10)
    assert "notification_gap_possible=true" in caplog.text
    await wait_for(
        lambda: (
            manager.status()["connected"]
            and manager.subscribed_channels == {room_channel(redis_prefix, r) for r in rooms}
        ),
        within=15,
    )
    assert manager.reference_counts == {rooms[0]: 2, rooms[1]: 1}  # counts survive the reconnect
    for room in rooms:
        assert await numsub(redis_client, redis_prefix, room) == 1
    await redis_client.publish(room_channel(redis_prefix, rooms[1]), envelope(rooms[1], seq=42))
    assert (await asyncio.wait_for(manager.queue.get(), timeout=3)).room_seq == 42  # live again
    assert "redis_pubsub_resubscribed" in caplog.text


async def test_notifications_published_while_disconnected_are_lost_not_recovered(
    manager, redis_client, redis_prefix
):
    room = uuid.uuid4()
    await manager.acquire(room)
    await redis_client.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
    # publish during the gap: nobody is subscribed, so Redis delivers to zero subscribers
    delivered_to = await redis_client.publish(
        room_channel(redis_prefix, room), envelope(room, seq=1)
    )
    await wait_for(
        lambda: (
            manager.status()["connected"]
            and room_channel(redis_prefix, room) in manager.subscribed_channels
        ),
        within=15,
    )
    await asyncio.sleep(0.3)
    if delivered_to == 0:  # (if the reconnect won the race there was no gap to lose it in)
        assert manager.queue.empty()  # restored subscriptions do not replay what was missed


async def test_status_reports_everything_an_operator_needs(manager):
    status = manager.status()
    assert set(status) >= {
        "connected",
        "subscribed_rooms",
        "rooms_with_connections",
        "connection_losses",
        "connect_failures",
        "dropped_notifications",
        "rejected_notifications",
        "notification_queue_depth",
    }


async def test_stop_fails_waiting_callers_and_closes_cleanly(settings, redis_client):
    queue: asyncio.Queue = asyncio.Queue()
    mgr = SubscriptionManager(settings, queue)
    await mgr.start()
    room = uuid.uuid4()
    await mgr.acquire(room)
    await mgr.stop()
    with pytest.raises(SubscribeFailed):
        await mgr.acquire(room)
    assert mgr.subscribed_channels == frozenset()
