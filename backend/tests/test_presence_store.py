"""The presence/typing Lua operations against a real Redis: atomicity, expiry, TTLs, availability.

No PostgreSQL is involved anywhere in this file.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio

from app.realtime.presence import PresenceStore, PresenceUnavailable
from app.realtime.publisher import create_redis_client
from tests.factories import make_settings

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(settings, redis_client):
    quick = settings.model_copy(
        update={"presence_expiry_seconds": 1.0, "typing_expiry_seconds": 1.0}
    )
    s = PresenceStore(quick)
    yield s
    await s.aclose()


def ids():
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()  # room, user, connection


async def users(store, room):
    return {u: a for u, a in await store.snapshot(room)}


async def test_one_user_with_many_tabs_is_one_online_user(store):
    room, alice, _ = ids()
    for _ in range(3):
        await store.register(room, alice, uuid.uuid4())
    assert list(await users(store, room)) == [alice]


async def test_closing_one_tab_does_not_show_offline_but_closing_the_last_does(store):
    room, alice, _ = ids()
    tab1, tab2 = uuid.uuid4(), uuid.uuid4()
    await store.register(room, alice, tab1)
    await store.register(room, alice, tab2)
    await store.remove(room, alice, tab1)
    assert list(await users(store, room)) == [alice]
    await store.remove(room, alice, tab2)
    assert await store.snapshot(room) == []


async def test_an_abrupt_disconnect_expires_by_itself_and_a_refresh_keeps_it(store):
    room, alice, _ = ids()
    bob = uuid.uuid4()
    a_conn, b_conn = uuid.uuid4(), uuid.uuid4()
    await store.register(room, alice, a_conn)
    await store.register(room, bob, b_conn)
    for _ in range(6):  # alice heartbeats; bob crashed (never refreshes)
        await asyncio.sleep(0.25)
        await store.register(room, alice, a_conn)
    assert list(await users(store, room)) == [alice]
    await asyncio.sleep(2.2)  # now alice stops too
    assert await store.snapshot(room) == []


async def test_scores_use_redis_time_not_the_api_servers_clock(store, redis_client):
    room, alice, conn = ids()
    await store.register(room, alice, conn)
    score = await redis_client.zscore(store.conns_key(room), store.member(alice, conn))
    seconds, micros = await redis_client.time()
    redis_now_ms = seconds * 1000 + micros // 1000
    assert abs((score - redis_now_ms) - 1000) < 1500  # expiry (1 s) after Redis's own clock


async def test_availability_is_per_user_shared_across_tabs_and_needs_a_live_connection(store):
    room, alice, _ = ids()
    assert await store.set_availability(room, alice, "busy") is False  # offline: refused
    assert await store.snapshot(room) == []
    tab1, tab2 = uuid.uuid4(), uuid.uuid4()
    await store.register(room, alice, tab1)
    await store.register(room, alice, tab2)
    assert await store.set_availability(room, alice, "dnd") is True
    assert await users(store, room) == {alice: "dnd"}  # one value for every tab
    await store.remove(room, alice, tab1)
    assert await users(store, room) == {alice: "dnd"}  # a tab left; still online, still dnd
    await store.set_availability(room, alice, "busy")  # last write wins
    assert await users(store, room) == {alice: "busy"}


async def test_availability_resets_to_available_after_going_offline(store, redis_client):
    room, alice, conn = ids()
    await store.register(room, alice, conn)
    await store.set_availability(room, alice, "dnd")
    await store.remove(room, alice, conn)
    assert not await redis_client.hget(store.avail_key(room), str(alice))  # dropped with the user
    await store.register(room, alice, uuid.uuid4())
    assert await users(store, room) == {alice: "available"}


async def test_availability_of_an_expired_user_is_dropped_even_without_a_remove(
    store, redis_client
):
    room, alice, conn = ids()
    await store.register(room, alice, conn)
    await store.set_availability(room, alice, "busy")
    await asyncio.sleep(2.2)  # crashed: the entry expired
    assert await store.snapshot(room) == []
    assert not await redis_client.hget(store.avail_key(room), str(alice))
    await store.register(room, alice, uuid.uuid4())  # back online: reset
    assert await users(store, room) == {alice: "available"}


async def test_availability_never_leaks_between_rooms(store):
    room_a, alice, _ = ids()
    room_b = uuid.uuid4()
    await store.register(room_a, alice, uuid.uuid4())
    await store.register(room_b, alice, uuid.uuid4())
    await store.set_availability(room_a, alice, "dnd")
    assert await users(store, room_b) == {alice: "available"}


async def test_typing_aggregates_by_user_and_one_tab_stopping_keeps_the_other(store):
    room, alice, bob = ids()
    tab1, tab2, b_tab = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await store.typing_start(room, alice, tab1)
    await store.typing_start(room, alice, tab2)
    await store.typing_start(room, bob, b_tab)
    assert set(await store.typing_snapshot(room)) == {alice, bob}
    assert len(await store.typing_snapshot(room)) == 2  # distinct users, not entries
    await store.typing_clear(room, alice, tab1)
    assert set(await store.typing_snapshot(room)) == {alice, bob}  # tab2 is still typing
    await store.typing_clear(room, alice, tab2)
    assert await store.typing_snapshot(room) == [bob]


async def test_typing_expires_by_itself(store):
    room, alice, conn = ids()
    await store.typing_start(room, alice, conn)
    assert await store.typing_snapshot(room) == [alice]
    await asyncio.sleep(2.2)
    assert await store.typing_snapshot(room) == []


async def test_every_key_carries_a_ttl(store, redis_client):
    room, alice, conn = ids()
    await store.register(room, alice, conn)
    await store.set_availability(room, alice, "busy")
    await store.typing_start(room, alice, conn)
    for key in (store.conns_key(room), store.avail_key(room), store.typing_key(room)):
        ttl = await redis_client.ttl(key)
        assert 0 < ttl <= 70, (key, ttl)  # never -1 (no expiry)
    keys = await redis_client.keys(f"{store._prefix}:*")
    assert keys
    for key in keys:
        assert await redis_client.ttl(key) > 0, key


async def test_operations_are_atomic_under_concurrency(store):
    """A keeper connection stays registered while 150 other connections of the SAME user are
    added and removed concurrently. At no instant may the user look offline, and the
    availability must survive (a non-atomic 'remove then check' would drop it)."""
    room, alice, keeper = ids()
    await store.register(room, alice, keeper)
    await store.set_availability(room, alice, "dnd")
    stop = asyncio.Event()
    observed_offline = []

    async def watcher():
        while not stop.is_set():
            snap = dict(await store.snapshot(room))
            if snap.get(alice) != "dnd":
                observed_offline.append(snap)
            await asyncio.sleep(0)

    gate = asyncio.Semaphore(25)  # the app itself never has more than ~6 Redis calls in flight

    async def churn(i):
        conn = uuid.uuid4()
        async with gate:
            await store.register(room, alice, conn)
            await store.typing_start(room, alice, conn)
        async with gate:
            await store.remove(room, alice, conn)

    watching = asyncio.create_task(watcher())
    await asyncio.gather(*[churn(i) for i in range(150)])
    stop.set()
    await watching
    assert observed_offline == []
    assert await users(store, room) == {alice: "dnd"}
    assert await store.typing_snapshot(room) == []


async def test_operations_survive_a_script_cache_flush(store, redis_client):
    room, alice, conn = ids()
    await store.register(room, alice, conn)
    await redis_client.script_flush()  # what a Redis restart or failover does
    await store.register(room, alice, conn)
    assert list(await users(store, room)) == [alice]


async def test_an_unreachable_redis_raises_presence_unavailable_not_offline():
    broken = make_settings(redis_url="redis://127.0.0.1:1/15", redis_socket_timeout_seconds=0.3)
    s = PresenceStore(broken, create_redis_client(broken))
    with pytest.raises(PresenceUnavailable):
        await s.snapshot(uuid.uuid4())
    with pytest.raises(PresenceUnavailable):
        await s.register(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    await s.aclose()
