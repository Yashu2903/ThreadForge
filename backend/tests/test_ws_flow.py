"""Acknowledgements, the unacked window, the heartbeat watchdog, rate limits and slow consumers.

Acks have NO durable effect: they drive flow control and diagnostics only (they are not read
receipts), and the reconnect cursor lives in the client. These tests prove the bounds hold.
"""

import asyncio
import uuid

import pytest

from app.realtime.protocol import CLOSE_SLOW_CONSUMER
from app.services.realtime_access import HandshakeIdentity
from tests.helpers import add_member, append_events, scalar
from tests.realtime_helpers import (
    FakeSocket,
    drain_until_closed,
    expect_no_event,
    live_server,
    open_synced,
    read_until,
    recv_event,
    recv_json,
    send_json,
    send_resume,
    wait_until,
    ws_connect,
)
from tests.test_invitations_api import new_room

pytestmark = pytest.mark.asyncio


def uid(value):
    return uuid.UUID(value)


# ---- ack validation ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after_seq", "ack"),
    [
        (3, 2),  # below the accepted resume point
        (3, 99),  # above last_sent (we never sent that)
        (0, "5"),  # malformed (string)
        (0, -1),
        (0, 1.5),
        (0, None),
        (0, True),
    ],
)
async def test_invalid_acks_close_4400(live, alice, sessionmaker, after_seq, ack):
    room_id = await new_room(alice)
    await append_events(sessionmaker, uid(room_id), 9)  # head 10
    ws, _ = await open_synced(live, room_id, alice, after_seq=after_seq if after_seq else 10)
    await send_json(ws, type="ack", through_seq=ack)
    _, code = await drain_until_closed(ws)
    assert code == 4400


async def test_a_backwards_ack_closes_4400_and_an_equal_ack_is_ignored(live, alice, sessionmaker):
    room_id = await new_room(alice)
    await append_events(sessionmaker, uid(room_id), 9)  # head 10
    ws = await ws_connect(live, room_id, alice.secret)
    await send_resume(ws, 0)
    frames = await read_until(ws, "sync_complete")
    assert [f["room_seq"] for f in frames if f["type"] == "room_event"] == list(range(1, 11))
    await send_json(ws, type="ack", through_seq=7)
    await send_json(ws, type="ack", through_seq=7)  # equal: ignored, the connection stays
    await send_json(ws, type="heartbeat")
    while (await recv_json(ws))["type"] != "heartbeat_ack":
        pass
    conn = next(iter(live.hub.rooms[uid(room_id)].connections.values()))
    assert conn.last_acked == 7
    await send_json(ws, type="ack", through_seq=5)  # backwards
    _, code = await drain_until_closed(ws)
    assert code == 4400


async def test_acks_have_no_durable_effect(live, alice, sessionmaker):
    room_id = await new_room(alice)
    await append_events(sessionmaker, uid(room_id), 4)
    tables = "rooms", "room_events", "outbox", "memberships", "sessions"
    before = {t: await scalar(sessionmaker, f"SELECT count(*) FROM {t}") for t in tables}
    seq_before = await scalar(sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id)
    ws = await ws_connect(live, room_id, alice.secret)
    await send_resume(ws, 0)
    await read_until(ws, "sync_complete")
    await send_json(ws, type="ack", through_seq=5)
    await asyncio.sleep(0.2)
    after = {t: await scalar(sessionmaker, f"SELECT count(*) FROM {t}") for t in tables}
    assert before == after
    assert seq_before == await scalar(
        sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id
    )
    await ws.close()
    # a NEW connection starting from 0 still gets everything: the cursor lives in the client
    ws, frames = await open_synced(live, room_id, alice, after_seq=0)
    assert len([f for f in frames if f["type"] == "room_event"]) == 5
    await ws.close()


async def test_acks_are_rate_limited(live, alice, sessionmaker):
    room_id = await new_room(alice)
    await append_events(sessionmaker, uid(room_id), 30)
    ws = await ws_connect(live, room_id, alice.secret)
    await send_resume(ws, 0)
    await read_until(ws, "sync_complete")
    for seq in range(1, 16):  # 15 acks back to back: well over 10 per second
        await send_json(ws, type="ack", through_seq=seq)
    _, code = await drain_until_closed(ws)
    assert code == CLOSE_SLOW_CONSUMER


async def test_a_flood_of_frames_closes_4429(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    for _ in range(100):
        await ws.send('{"type":"heartbeat"}')
    _, code = await drain_until_closed(ws)
    assert code == 4429


# ---- the unacked window --------------------------------------------------------------------------------------


async def test_the_unacked_window_pauses_draining_until_an_ack_arrives(
    settings, engine, redis_client, make_actor, sessionmaker
):
    small = settings.model_copy(update={"ack_window": 5, "replay_page_size": 3})
    alice = await make_actor("Alice")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        await append_events(sessionmaker, uid(room_id), 19)  # 20 events
        ws = await ws_connect(server, room_id, alice.secret)
        await send_resume(ws, 0)
        got = [await recv_json(ws)]  # sync_started
        for _ in range(5):
            got.append(await recv_json(ws))
        assert [f["room_seq"] for f in got[1:]] == [1, 2, 3, 4, 5]
        await expect_no_event(ws, 0.5)  # paused: exactly the window, never more
        conn = next(iter(server.hub.rooms[uid(room_id)].connections.values()))
        assert conn.last_sent - conn.last_acked == 5 and server.hub.counters.window_pauses >= 1
        await send_json(ws, type="ack", through_seq=5)
        more = [await recv_json(ws) for _ in range(5)]
        assert [f["room_seq"] for f in more] == [6, 7, 8, 9, 10]
        await expect_no_event(ws, 0.3)
        await ws.close()


async def test_the_window_bounds_memory_for_a_client_that_never_acks(
    settings, engine, redis_client, make_actor, sessionmaker
):
    small = settings.model_copy(update={"ack_window": 8, "replay_page_size": 5})
    alice = await make_actor("Alice")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        await append_events(sessionmaker, uid(room_id), 200)
        ws = await ws_connect(server, room_id, alice.secret)
        await send_resume(ws, 0)
        await wait_until(lambda: server.hub.counters.window_pauses >= 1)
        conn = next(iter(server.hub.rooms[uid(room_id)].connections.values()))
        assert len(conn.outbox) <= 9 + 16 and conn.last_sent == 8  # bounded, whatever the backlog
        await ws.close()


async def test_stalled_acks_close_4429(settings, engine, redis_client, make_actor, sessionmaker):
    small = settings.model_copy(
        update={"ack_window": 3, "ack_timeout_seconds": 0.6, "ws_send_timeout_seconds": 0.3}
    )
    alice = await make_actor("Alice")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        await append_events(sessionmaker, uid(room_id), 10)
        ws = await ws_connect(server, room_id, alice.secret)
        await send_resume(ws, 0)
        frames, code = await drain_until_closed(ws)
        assert code == 4429
        assert [f["room_seq"] for f in frames if f["type"] == "room_event"] == [1, 2, 3]
        assert server.hub.counters.ack_timeouts == 1


async def test_a_paused_replay_does_not_trip_the_heartbeat_watchdog(
    settings, engine, redis_client, make_actor, sessionmaker
):
    small = settings.model_copy(
        update={
            "ack_window": 3,
            "ack_timeout_seconds": 5,
            "ws_watchdog_timeout_seconds": 0.8,
            "client_heartbeat_interval_seconds": 0.2,
            "presence_expiry_seconds": 1.0,
        }
    )
    alice = await make_actor("Alice")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        await append_events(sessionmaker, uid(room_id), 10)
        ws = await ws_connect(server, room_id, alice.secret)
        await send_resume(ws, 0)
        for _ in range(8):  # 1.6 s of replay stuck on the window, but heartbeats keep flowing
            await ws.send('{"type":"heartbeat"}')
            await asyncio.sleep(0.2)
        assert ws.close_code is None
        assert server.hub.counters.watchdog_closes == 0 and server.hub.counters.window_pauses >= 1
        await ws.close()


# ---- the heartbeat watchdog --------------------------------------------------------------------------------


async def test_silence_after_sync_closes_4408(settings, engine, redis_client, make_actor):
    small = settings.model_copy(
        update={
            "ws_watchdog_timeout_seconds": 0.6,
            "client_heartbeat_interval_seconds": 0.2,
            "presence_expiry_seconds": 1.0,
        }
    )
    alice = await make_actor("Alice")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        ws, _ = await open_synced(server, room_id, alice, after_seq=1)
        _, code = await drain_until_closed(ws)
        assert code == 4408 and server.hub.counters.watchdog_closes == 1
        assert server.hub.active_connections == 0


async def test_heartbeats_keep_a_quiet_connection_alive(settings, engine, redis_client, make_actor):
    small = settings.model_copy(
        update={
            "ws_watchdog_timeout_seconds": 0.6,
            "client_heartbeat_interval_seconds": 0.2,
            "presence_expiry_seconds": 1.0,
        }
    )
    alice = await make_actor("Alice")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        ws, _ = await open_synced(server, room_id, alice, after_seq=1)
        for _ in range(8):
            await ws.send('{"type":"heartbeat"}')
            await asyncio.sleep(0.2)
        assert ws.close_code is None
        await ws.close()


# ---- slow consumers ------------------------------------------------------------------------------------------


async def test_a_stalled_peer_is_closed_4429_without_blocking_anyone_else(
    settings, engine, redis_client, make_actor, sessionmaker
):
    small = settings.model_copy(
        update={"ws_send_timeout_seconds": 0.3, "ws_connection_queue_size": 5, "ack_window": 50}
    )
    alice, bob = await make_actor("Alice"), await make_actor("Bob")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id)
        stalled = FakeSocket()
        stalled.gate.clear()  # the peer stopped reading: every send blocks
        identity = HandshakeIdentity(
            user_id=bob.id,
            session_id=await scalar(
                sessionmaker, "SELECT id FROM sessions WHERE user_id = :u", u=bob.id
            ),
        )
        stalled.feed(type="resume", schema_version=1, after_seq=0)
        serve = asyncio.create_task(server.hub.serve(stalled, uid(room_id), identity))
        healthy, _ = await open_synced(server, room_id, alice, after_seq=1)
        await append_events(sessionmaker, uid(room_id), 30)
        server.hub.on_room_hint(uid(room_id), 31)
        got = []
        while len(got) < 30:
            got.append((await recv_event(healthy, 5))["room_seq"])
            if len(got) % 25 == 0:
                await send_json(healthy, type="ack", through_seq=got[-1])
        assert got == list(range(2, 32))  # the healthy peer missed nothing
        await asyncio.wait_for(serve, timeout=5)
        assert stalled.closed_with == 4429 and server.hub.counters.send_timeouts >= 1
        assert server.hub.active_connections == 1
        await healthy.close()


async def test_a_full_outgoing_queue_applies_backpressure_not_unbounded_memory(
    settings, engine, redis_client, make_actor, sessionmaker
):
    small = settings.model_copy(
        update={"ws_send_timeout_seconds": 5, "ws_connection_queue_size": 4, "ack_window": 100}
    )
    alice = await make_actor("Alice")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        await append_events(sessionmaker, uid(room_id), 60)
        slow = FakeSocket()
        slow.gate.clear()
        identity = HandshakeIdentity(
            alice.id,
            await scalar(sessionmaker, "SELECT id FROM sessions WHERE user_id = :u", u=alice.id),
        )
        slow.feed(type="resume", schema_version=1, after_seq=0)
        serve = asyncio.create_task(server.hub.serve(slow, uid(room_id), identity))
        await wait_until(lambda: server.hub.rooms.get(uid(room_id)) is not None)
        conn = next(iter(server.hub.rooms[uid(room_id)].connections.values()))
        await asyncio.sleep(0.5)
        assert len(conn.outbox) <= 4 and conn.last_sent <= 5  # blocked, bounded
        slow.gate.set()  # the peer wakes up and drains
        await wait_until(lambda: len(slow.sent) >= 10)
        slow.disconnect()
        await asyncio.wait_for(serve, timeout=5)
        seqs = [f["room_seq"] for f in slow.sent if f["type"] == "room_event"]
        assert seqs == sorted(seqs) and seqs == list(range(1, len(seqs) + 1))  # order preserved
