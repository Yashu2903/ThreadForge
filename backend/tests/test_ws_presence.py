"""Presence and typing over real WebSockets, real Redis and real PostgreSQL.

Intervals are shrunk in `fast` (presence expiry 1.2 s, tick 0.3 s, typing expiry 1 s) and every wait
is a wait-until-condition with a timeout, never a fixed sleep.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio

from tests.helpers import add_member, scalar
from tests.realtime_helpers import (
    live_server,
    open_synced,
    recv_json,
    send_json,
    wait_until,
)
from tests.test_invitations_api import new_room
from tests.test_members_api import member_path

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fast(settings):
    return settings.model_copy(
        update={
            "presence_expiry_seconds": 1.2,
            "presence_tick_seconds": 0.3,
            "presence_hint_min_interval_seconds": 0.05,
            "typing_expiry_seconds": 1.0,
            "typing_state_min_interval_seconds": 0.05,
            "client_heartbeat_interval_seconds": 0.5,
            "ws_watchdog_timeout_seconds": 5.0,
            "reconcile_interval_seconds": 0.5,
            "reconcile_interval_degraded_seconds": 0.3,
        }
    )


@pytest_asyncio.fixture
async def server(fast, engine, redis_client):
    async with live_server(fast, engine) as s:
        yield s


class Watcher:
    """Reads a socket in the background and keeps the LAST presence_snapshot / typing_state and a
    history of both, so tests can wait for a condition instead of sleeping."""

    def __init__(self, ws):
        self.ws = ws
        self.presence: dict | None = None
        self.typing: dict | None = None
        self.presence_history: list[dict] = []
        self.typing_history: list[dict] = []
        self.events: list[dict] = []
        self.closed_code: int | None = None
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            while True:
                frame = await recv_json(self.ws, 60)
                if frame["type"] == "presence_snapshot":
                    self.presence = frame
                    self.presence_history.append(frame)
                elif frame["type"] == "typing_state":
                    self.typing = frame
                    self.typing_history.append(frame)
                elif frame["type"] == "room_event":
                    self.events.append(frame)
        except Exception as exc:  # noqa: BLE001 - the socket closed
            self.closed_code = getattr(getattr(exc, "rcvd", None), "code", None)

    def online(self) -> dict[str, str]:
        return {
            e["user_id"]: e["availability"] for e in (self.presence or {"online": []})["online"]
        }

    def typists(self) -> list[str]:
        return (self.typing or {"typing": []})["typing"]

    async def close(self):
        await self.ws.close()
        self.task.cancel()


async def watch(server, room_id, actor, after_seq=1):
    ws, _ = await open_synced(server, room_id, actor, after_seq=after_seq)
    return Watcher(ws)


async def setup_room(sessionmaker, alice, *others, role="editor"):
    room_id = await new_room(alice)
    for user in others:
        await add_member(sessionmaker, room_id, user.id, role)
    return room_id


# ---- presence --------------------------------------------------------------------------------------------


async def test_a_new_connection_gets_the_first_snapshots(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    a = await watch(server, room_id, alice)
    await wait_until(lambda: a.presence is not None and a.typing is not None)
    assert a.online() == {str(alice.id): "available"} and a.presence["degraded"] is False
    assert a.typing == {
        "type": "typing_state",
        "room_id": room_id,
        "typing": [],
        "degraded": False,
    }
    b = await watch(server, room_id, bob)
    await wait_until(lambda: set(a.online()) == {str(alice.id), str(bob.id)})
    await wait_until(lambda: b.presence is not None and len(b.online()) == 2)
    await a.close()
    await b.close()


async def test_two_tabs_are_one_online_user_and_closing_one_does_not_show_offline(
    server, alice, bob, sessionmaker
):
    room_id = await setup_room(sessionmaker, alice, bob)
    a = await watch(server, room_id, alice)
    tab1, tab2 = await watch(server, room_id, bob), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    assert list(a.online()).count(str(bob.id)) == 1  # one user, not two
    await tab1.close()
    seen = len(a.presence_history)
    await asyncio.sleep(0.8)  # several ticks
    assert str(bob.id) in a.online()
    assert all(
        str(bob.id) in {e["user_id"] for e in f["online"]} for f in a.presence_history[seen:]
    )
    await tab2.close()
    await wait_until(lambda: str(bob.id) not in a.online())
    await a.close()


async def test_a_crashed_connection_expires_by_itself(server, alice, bob, sessionmaker):
    """No clean disconnect and no cleanup ever runs: only the TTL removes the entry (simulated by
    registering a member straight in Redis and never touching it again)."""
    room_id = await setup_room(sessionmaker, alice, bob)
    a = await watch(server, room_id, alice)
    await server.hub.presence.store.register(uuid.UUID(room_id), bob.id, uuid.uuid4())
    await server.hub.presence.store.publish_hint(uuid.UUID(room_id))
    await wait_until(lambda: str(bob.id) in a.online())
    await wait_until(
        lambda: str(bob.id) not in a.online(), within=6
    )  # expires, then a tick says so
    await a.close()


async def test_heartbeats_keep_a_quiet_connection_online(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    for _ in range(8):  # 2.4 s, twice the 1.2 s expiry
        await send_json(b.ws, type="heartbeat")
        await asyncio.sleep(0.3)
    assert str(bob.id) in a.online()
    await a.close()
    await b.close()


async def test_a_silent_connection_is_closed_by_the_watchdog_and_goes_offline(
    fast, engine, redis_client, alice, bob, sessionmaker
):
    tight = fast.model_copy(
        update={"ws_watchdog_timeout_seconds": 1.0, "client_heartbeat_interval_seconds": 0.4}
    )
    async with live_server(tight, engine) as server:
        room_id = await setup_room(sessionmaker, alice, bob)
        a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
        await wait_until(lambda: str(bob.id) in a.online())
        for _ in range(8):  # alice stays alive; bob says nothing
            await send_json(a.ws, type="heartbeat")
            await asyncio.sleep(0.3)
        assert b.task.done() and b.closed_code == 4408
        await wait_until(lambda: str(bob.id) not in a.online())
        await a.close()


async def test_availability_is_shared_by_tabs_and_never_affects_delivery(
    server, alice, bob, sessionmaker
):
    room_id = await setup_room(sessionmaker, alice, bob)
    a = await watch(server, room_id, alice)
    tab1, tab2 = await watch(server, room_id, bob), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    await send_json(tab1.ws, type="set_availability", value="dnd")
    await wait_until(lambda: a.online().get(str(bob.id)) == "dnd")
    await wait_until(lambda: tab2.online().get(str(bob.id)) == "dnd")  # the OTHER tab sees it too
    await send_json(tab2.ws, type="set_availability", value="busy")  # last write wins
    await wait_until(lambda: a.online().get(str(bob.id)) == "busy")
    # a do-not-disturb user still receives every event
    await send_json(tab1.ws, type="set_availability", value="dnd")
    from tests.test_messages_send import send

    await send(alice, room_id, "still delivered")
    await wait_until(
        lambda: any(e["event_type"] == "message_created" for e in tab1.events), within=6
    )
    await a.close()
    await tab1.close()
    await tab2.close()


async def test_availability_resets_when_the_user_reconnects(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    await send_json(b.ws, type="set_availability", value="dnd")
    await wait_until(lambda: a.online().get(str(bob.id)) == "dnd")
    await b.close()
    await wait_until(lambda: str(bob.id) not in a.online())
    b2 = await watch(server, room_id, bob)
    await wait_until(lambda: a.online().get(str(bob.id)) == "available")
    await a.close()
    await b2.close()


async def test_removed_users_never_appear_in_snapshots(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    a, _b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    response = await alice.delete(member_path(room_id, bob))
    assert response.status_code in (200, 204)
    await wait_until(lambda: str(bob.id) not in a.online(), within=6)
    gone_at = len(a.presence_history)  # from here on bob must never be listed again
    # even a stale Redis entry for a non-member is filtered out of every later snapshot
    await server.hub.presence.store.register(uuid.UUID(room_id), bob.id, uuid.uuid4())
    await server.hub.presence.store.publish_hint(uuid.UUID(room_id))
    await asyncio.sleep(0.8)
    assert str(bob.id) not in a.online()
    later = a.presence_history[gone_at - 1 :]
    assert all(str(bob.id) not in {e["user_id"] for e in f["online"]} for f in later)
    await a.close()


# ---- typing -----------------------------------------------------------------------------------------------


async def test_typing_is_seen_by_others_and_expires(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    await send_json(b.ws, type="typing", state="start")
    await wait_until(lambda: a.typists() == [str(bob.id)])
    await wait_until(lambda: a.typists() == [], within=5)  # expires after 1 s with no refresh
    await a.close()
    await b.close()


async def test_typing_stop_clears_it_and_works_for_a_viewer(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob, role="viewer")  # chat is open to viewers
    a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    await send_json(b.ws, type="typing", state="start")
    await wait_until(lambda: a.typists() == [str(bob.id)])
    await send_json(b.ws, type="typing", state="stop")
    await wait_until(lambda: a.typists() == [])
    await a.close()
    await b.close()


async def test_one_tabs_stop_does_not_clear_another_tabs_typing(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    a = await watch(server, room_id, alice)
    tab1, tab2 = await watch(server, room_id, bob), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    await send_json(tab1.ws, type="typing", state="start")
    await send_json(tab2.ws, type="typing", state="start")
    await wait_until(lambda: a.typists() == [str(bob.id)])
    await send_json(tab1.ws, type="typing", state="stop")
    await asyncio.sleep(0.4)
    assert a.typists() == [str(bob.id)]  # tab2 is still typing
    await send_json(tab2.ws, type="typing", state="stop")
    await wait_until(lambda: a.typists() == [])
    await a.close()
    await tab1.close()
    await tab2.close()


async def test_typing_frames_are_throttled_per_connection(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    b = await watch(server, room_id, bob)
    await wait_until(lambda: b.presence is not None)
    for _ in range(20):  # a burst far above 2 per second
        await send_json(b.ws, type="typing", state="start")
    await wait_until(lambda: server.hub.counters.typing_dropped >= 10)
    assert not b.task.done()  # dropped and counted, not fatal
    await b.close()


async def test_typing_state_is_coalesced_per_room(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    before = len(a.typing_history)
    for state in ("start", "stop") * 4:
        await send_json(b.ws, type="typing", state=state)
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.6)
    assert len(a.typing_history) - before <= 4  # far fewer frames than the 8 changes
    assert a.typists() == []
    await a.close()
    await b.close()


# ---- nothing durable --------------------------------------------------------------------------------------


async def test_presence_and_typing_never_touch_postgresql(server, alice, bob, sessionmaker):
    room_id = await setup_room(sessionmaker, alice, bob)
    tables = ("rooms", "room_events", "outbox", "memberships", "messages", "sessions")

    async def counts():
        return {t: await scalar(sessionmaker, f"SELECT count(*) FROM {t}") for t in tables} | {
            "seq": await scalar(
                sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id
            )
        }

    before = await counts()
    a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    await send_json(b.ws, type="typing", state="start")
    await send_json(b.ws, type="set_availability", value="busy")
    await wait_until(lambda: a.online().get(str(bob.id)) == "busy" and a.typists())
    await b.close()
    await wait_until(lambda: str(bob.id) not in a.online())
    assert before == await counts()
    await a.close()


# ---- Redis trouble ------------------------------------------------------------------------------------------


async def test_redis_failures_report_degraded_not_offline_and_recovery_rebuilds_presence(
    server, alice, bob, sessionmaker
):
    room_id = await setup_room(sessionmaker, alice, bob)
    a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    store = server.hub.presence.store
    original = store._run

    async def failing(coro):  # TEST-ONLY fault injection: Redis stops answering
        coro.close()
        from app.realtime.presence import PresenceUnavailable

        raise PresenceUnavailable("injected")

    store._run = failing
    server.hub.presence.on_redis_lost()
    await wait_until(lambda: a.presence["degraded"] is True and a.typing["degraded"] is True)
    assert a.presence["online"] == [] and a.typing["typing"] == []  # unknown, NOT "everyone left"
    store._run = original  # Redis is back
    await wait_until(
        lambda: a.presence["degraded"] is False and str(bob.id) in a.online(), within=8
    )
    assert {str(alice.id), str(bob.id)} <= set(a.online())
    await a.close()
    await b.close()


async def test_a_redis_flush_is_rebuilt_from_the_live_connections(
    server, alice, bob, sessionmaker, redis_client
):
    room_id = await setup_room(sessionmaker, alice, bob)
    a, b = await watch(server, room_id, alice), await watch(server, room_id, bob)
    await wait_until(lambda: str(bob.id) in a.online())
    key = server.hub.presence.store.conns_key(uuid.UUID(room_id))
    assert await redis_client.zcard(key) == 2
    await redis_client.flushdb()  # test index 15 only: every presence key is gone
    assert await redis_client.zcard(key) == 0  # really gone (not just a stale snapshot)

    async def rebuilt():
        return await redis_client.zcard(key) == 2

    await wait_until(rebuilt, within=8)  # the live connections re-registered themselves
    await wait_until(lambda: {str(alice.id), str(bob.id)} <= set(a.online()), within=8)
    await a.close()
    await b.close()


async def test_websockets_work_in_degraded_mode_when_redis_is_unreachable(
    fast, engine, make_actor, sessionmaker
):
    broken = fast.model_copy(
        update={
            "redis_url": type(fast.redis_url)("redis://127.0.0.1:1/15"),
            "redis_socket_timeout_seconds": 0.3,
            "ws_subscribe_timeout_seconds": 0.5,
        }
    )
    alice = await make_actor("Alice")
    async with live_server(broken, engine) as server:
        room_id = await new_room(alice)
        a = await watch(server, room_id, alice)
        await wait_until(lambda: a.presence is not None and a.typing is not None, within=8)
        assert a.presence["degraded"] is True and a.presence["online"] == []
        assert a.typing["degraded"] is True
        await send_json(a.ws, type="typing", state="start")  # accepted (and silently unusable)
        await asyncio.sleep(0.3)
        assert not a.task.done()
        await a.close()


async def test_leaving_cleans_up_presence_and_leaks_nothing(
    server, alice, bob, sessionmaker, redis_client
):
    room_id = await setup_room(sessionmaker, alice, bob)
    conns = []
    for _ in range(4):
        conns.append(await watch(server, room_id, bob))
    for c in conns:
        await c.close()
    await wait_until(lambda: server.hub.active_connections == 0)
    store = server.hub.presence.store

    async def gone():
        return await redis_client.zcard(store.conns_key(uuid.UUID(room_id))) == 0

    await wait_until(gone)
    assert server.hub.presence.ops.qsize() == 0
