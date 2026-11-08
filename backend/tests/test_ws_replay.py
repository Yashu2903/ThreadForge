"""Ordered replay, the switch to live, hints and reconciliation (checkpoint 2).

Every test uses a real uvicorn server, a real WebSocket client, real PostgreSQL and real Redis.
Events are usually appended with `append_events`, which commits them exactly like a service
function does (room lock, event_seq bump, room_events rows) but publishes no hint: delivery then
depends only on replay, reconciliation or an explicit wake, which is what is being tested.
"""

import asyncio
import json
import logging
import uuid

import pytest

from app.services import realtime_access
from tests.helpers import add_member, append_events, scalar
from tests.realtime_helpers import (
    drain_until_closed,
    expect_no_event,
    live_server,
    open_synced,
    read_sync_acking,
    recv_event,
    send_json,
    send_resume,
    wait_until,
    ws_connect,
)
from tests.test_invitations_api import new_room
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio


def seqs(frames):
    return [f["room_seq"] for f in frames if f["type"] == "room_event"]


async def test_a_client_at_41_receives_42_and_43(live, alice, sessionmaker):
    room_id = await new_room(alice)
    await append_events(sessionmaker, uuid_of(room_id), 42)  # seq 2..43
    ws, frames = await open_synced(live, room_id, alice, after_seq=41)
    assert [f["type"] for f in frames] == [
        "sync_started",
        "room_event",
        "room_event",
        "sync_complete",
    ]
    assert seqs(frames) == [42, 43]
    assert frames[0]["after_seq"] == 41 and frames[0]["through_seq"] == 43
    assert frames[-1]["through_seq"] == 43
    await ws.close()


def uuid_of(value):
    return uuid.UUID(value)


async def test_multiple_replay_pages_stay_ordered_and_gapless(live, alice, sessionmaker):
    room_id = await new_room(alice)
    await append_events(sessionmaker, uuid_of(room_id), 349)  # 350 events, 4 pages of <= 100
    ws = await ws_connect(live, room_id, alice.secret)
    await send_resume(ws, 0)
    frames = await read_sync_acking(ws)
    assert seqs(frames) == list(range(1, 351))
    assert frames[0]["through_seq"] == 350 and frames[-1] == {
        "type": "sync_complete",
        "through_seq": 350,
    }
    await ws.close()


async def test_an_event_committed_during_replay_arrives_afterwards_without_a_gap(
    live, alice, sessionmaker, monkeypatch
):
    room_id = await new_room(alice)
    await append_events(sessionmaker, uuid_of(room_id), 150)  # 151 events: two replay pages
    original = realtime_access.fetch_events_page
    state = {"calls": 0}

    async def racing(sm, room, after, limit):
        result = await original(sm, room, after, limit)
        state["calls"] += 1
        if state["calls"] == 1:  # a write commits between the first and second page
            await append_events(sessionmaker, room, 5)
        return result

    monkeypatch.setattr(realtime_access, "fetch_events_page", racing)
    ws = await ws_connect(live, room_id, alice.secret)
    await send_resume(ws, 0)
    frames = await read_sync_acking(ws)
    assert frames[-1]["through_seq"] == 151  # the replay target H was fixed up front
    assert seqs(frames) == list(range(1, 152))
    late = [await recv_event(ws) for _ in range(5)]  # delivered after sync_complete
    assert [f["room_seq"] for f in late] == [152, 153, 154, 155, 156]
    await ws.close()


async def test_duplicate_and_out_of_order_hints_still_give_ordered_unique_delivery(
    live, alice, sessionmaker
):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    await append_events(sessionmaker, uuid_of(room_id), 6)  # seq 2..7
    room = uuid_of(room_id)
    for seq in (7, 3, 7, 5, 2, 7, 4, 6, 1, 7):  # shuffled, repeated, even old hints
        live.hub.on_room_hint(room, seq)
    got = [(await recv_event(ws))["room_seq"] for _ in range(6)]
    assert got == [2, 3, 4, 5, 6, 7]
    await expect_no_event(ws, 0.4)
    await ws.close()


async def test_a_lost_final_hint_is_found_by_reconciliation(
    settings, engine, make_actor, sessionmaker
):
    fast = settings.model_copy(update={"reconcile_interval_seconds": 0.3})
    alice = await make_actor("Alice")
    async with live_server(fast, engine) as server:
        room_id = await new_room(alice)
        ws, _ = await open_synced(server, room_id, alice, after_seq=1)
        server.hub.suppress_hints = True  # every hint is lost
        await append_events(sessionmaker, uuid_of(room_id), 1)
        event = await recv_event(ws, 3)  # within about one reconcile interval
        assert event["room_seq"] == 2
        assert server.hub.counters.hints_applied == 0
        assert server.hub.counters.reconcile_wakes >= 1
        await ws.close()


async def test_reconciliation_is_one_query_for_all_rooms_and_connections(
    live, alice, bob, sessionmaker, monkeypatch
):
    rooms = [await new_room(alice, f"R{i}") for i in range(3)]
    sockets = [await open_synced(live, r, alice, after_seq=1) for r in rooms]
    sockets.append(await open_synced(live, rooms[0], alice, after_seq=1))
    calls = []
    original = realtime_access.read_event_seqs

    async def counting(sm, room_ids):
        calls.append(sorted(room_ids))
        return await original(sm, room_ids)

    monkeypatch.setattr(realtime_access, "read_event_seqs", counting)
    await live.hub.reconcile_once()
    assert len(calls) == 1 and len(calls[0]) == 3  # 3 rooms, 4 connections, ONE statement
    for ws, _ in sockets:
        await ws.close()


async def test_a_redis_resubscription_wakes_every_connection(live, alice, sessionmaker):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    live.hub.suppress_hints = True
    await append_events(sessionmaker, uuid_of(room_id), 1)
    await expect_no_event(ws, 0.3)  # nothing wakes it (reconcile is 5 s away)
    live.hub.manager._recovered()  # what a re-established subscription does
    event = await recv_event(ws, 2)
    assert event["room_seq"] == 2
    await ws.close()


async def test_every_connection_to_a_room_sees_the_same_order(live, alice, bob, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id)
    sockets = [await open_synced(live, room_id, u, after_seq=1) for u in (alice, bob, alice, bob)]
    await append_events(sessionmaker, uuid_of(room_id), 30)
    live.hub.on_room_hint(uuid_of(room_id), 31)
    results = []
    for ws, _ in sockets:
        results.append([(await recv_event(ws))["room_seq"] for _ in range(30)])
        await ws.close()
    assert all(r == list(range(2, 32)) for r in results)


async def test_contiguity_holds_under_concurrent_writers(
    settings, engine, redis_client, make_actor, sessionmaker
):
    """Race many real HTTP sends from three users; the drain output has no gap and ends at the
    room's event_seq. (Sequence numbers are allocated under the room lock, held until commit.)"""
    big = settings.model_copy(update={"message_writes_per_minute": 1000})
    users = [await make_actor(n, app_settings=big) for n in ("A", "B", "C")]
    async with live_server(big, engine) as server:
        room_id = await new_room(users[0])
        await add_member(sessionmaker, room_id, users[1].id)
        await add_member(sessionmaker, room_id, users[2].id)
        ws, _ = await open_synced(server, room_id, users[0], after_seq=1)

        async def writer(user, tag):
            for i in range(12):
                response = await send(user, room_id, f"{tag}{i}")
                assert response.status_code == 201

        await asyncio.gather(*[writer(u, t) for u, t in zip(users, "abc", strict=True)])
        got = []
        while len(got) < 36:
            got.append((await recv_event(ws, 10))["room_seq"])
            if len(got) % 20 == 0:
                await send_json(ws, type="ack", through_seq=got[-1])
        assert got == list(range(2, 38))
        assert (
            await scalar(sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id) == 37
        )
        await ws.close()


# ---- cursors that cannot be served ------------------------------------------------------------------


async def test_cursor_ahead_sends_resync_required_then_closes_4410(live, alice):
    room_id = await new_room(alice)  # event_seq 1
    ws = await ws_connect(live, room_id, alice.secret)
    await send_resume(ws, 2)
    frames, code = await drain_until_closed(ws)
    assert frames == [{"type": "resync_required", "reason": "cursor_ahead"}]
    assert code == 4410
    assert live.hub.counters.resyncs["cursor_ahead"] == 1


async def test_too_far_behind_sends_resync_required_then_closes_4410(
    settings, engine, redis_client, make_actor, sessionmaker
):
    small = settings.model_copy(update={"replay_max_events": 20, "replay_page_size": 10})
    alice = await make_actor("Alice")
    async with live_server(small, engine) as server:
        room_id = await new_room(alice)
        await append_events(sessionmaker, uuid_of(room_id), 30)  # head 31
        ws = await ws_connect(server, room_id, alice.secret)
        await send_resume(ws, 5)  # 26 behind > 20
        frames, code = await drain_until_closed(ws)
        assert frames == [{"type": "resync_required", "reason": "too_far_behind"}] and code == 4410
        ws = await ws_connect(server, room_id, alice.secret)
        await send_resume(ws, 11)  # exactly 20 behind: allowed
        frames = await __import__("tests.realtime_helpers", fromlist=["x"]).read_until(
            ws, "sync_complete"
        )
        assert seqs(frames) == list(range(12, 32))
        await ws.close()


async def test_an_injected_gap_gives_event_gap_and_logs_an_error(
    live, alice, sessionmaker, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR, logger="threadforge")
    room_id = await new_room(alice)
    await append_events(sessionmaker, uuid_of(room_id), 5)  # seq 1..6
    original = realtime_access.fetch_events_page

    async def with_a_hole(sm, room, after, limit):  # TEST-ONLY fault injection: lose seq 3
        return [e for e in await original(sm, room, after, limit) if e.room_seq != 3]

    monkeypatch.setattr(realtime_access, "fetch_events_page", with_a_hole)
    ws = await ws_connect(live, room_id, alice.secret)
    await send_resume(ws, 0)
    frames, code = await drain_until_closed(ws)
    assert frames[-1] == {"type": "resync_required", "reason": "event_gap"} and code == 4410
    assert all(s < 3 for s in seqs(frames))  # NOTHING at or after the hole was sent: never skipped
    text = caplog.text
    assert "event_gap" in text and "expected_seq=3" in text and "found_seq=4" in text
    assert str(room_id) in text
    assert live.hub.counters.resyncs["event_gap"] == 1


# ---- the first frame ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "resume", "schema_version": 1, "after_seq": -1},
        {"type": "resume", "schema_version": 1, "after_seq": "3"},
        {"type": "resume", "schema_version": 2, "after_seq": 0},
        {"type": "resume", "schema_version": 1, "after_seq": 0, "room_id": "x"},
        {"type": "resume", "after_seq": 0},
        {"type": "ack", "through_seq": 0},
    ],
)
async def test_a_bad_first_frame_closes_4400(live, alice, payload):
    room_id = await new_room(alice)
    ws = await ws_connect(live, room_id, alice.secret)
    await send_json(ws, **payload)
    _, code = await drain_until_closed(ws)
    assert code == 4400


async def test_no_resume_within_the_timeout_closes_4408(settings, engine, redis_client, make_actor):
    quick = settings.model_copy(update={"ws_resume_timeout_seconds": 0.3})
    alice = await make_actor("Alice")
    async with live_server(quick, engine) as server:
        room_id = await new_room(alice)
        ws = await ws_connect(server, room_id, alice.secret)
        _, code = await drain_until_closed(ws)
        assert code == 4408
        assert server.hub.active_connections == 0


# ---- reconnect storms -----------------------------------------------------------------------------------


async def test_replay_concurrency_is_limited(
    settings, engine, redis_client, make_actor, sessionmaker, monkeypatch
):
    limited = settings.model_copy(
        update={"replay_concurrency": 2, "ws_max_connections_per_user": 20}
    )
    users = [await make_actor(f"U{i}") for i in range(6)]
    async with live_server(limited, engine) as server:
        room_id = await new_room(users[0])
        for u in users[1:]:
            await add_member(sessionmaker, room_id, u.id)
        await append_events(sessionmaker, uuid_of(room_id), 150)
        original = realtime_access.fetch_events_page

        async def slow(sm, room, after, limit):
            await asyncio.sleep(0.15)  # keep each replay in flight long enough to overlap
            return await original(sm, room, after, limit)

        monkeypatch.setattr(realtime_access, "fetch_events_page", slow)

        async def client(user):
            ws = await ws_connect(server, room_id, user.secret)
            await send_resume(ws, 0)
            frames = await read_sync_acking(ws, within=15)
            await ws.close()
            return seqs(frames)

        results = await asyncio.gather(*[client(u) for u in users])
        assert all(r == list(range(1, 152)) for r in results)  # everyone converged
        assert server.hub.counters.replay_peak == 2  # but never more than 2 at a time
        assert server.hub.replaying == 0


async def test_a_connection_that_cannot_get_a_replay_slot_closes_1013(
    settings, engine, redis_client, make_actor, sessionmaker
):
    tight = settings.model_copy(
        update={
            "replay_concurrency": 1,
            "replay_slot_wait_seconds": 0.4,
            "ack_window": 5,
            "ack_timeout_seconds": 30,
        }
    )
    users = [await make_actor(f"U{i}") for i in range(2)]
    async with live_server(tight, engine) as server:
        room_id = await new_room(users[0])
        await add_member(sessionmaker, room_id, users[1].id)
        await append_events(sessionmaker, uuid_of(room_id), 20)
        hog = await ws_connect(server, room_id, users[0].secret)
        await send_resume(hog, 0)  # never acks: stuck in replay, holding the only slot
        await wait_until(lambda: server.hub.replaying == 1)
        late = await ws_connect(server, room_id, users[1].secret)
        await send_resume(late, 0)
        _, code = await drain_until_closed(late)
        assert code == 1013
        assert server.hub.counters.replay_slot_timeouts == 1
        await hog.close()


async def test_mass_reconnect_leaks_nothing(live, alice, redis_client, settings, sessionmaker):
    room_id = await new_room(alice)
    await append_events(sessionmaker, uuid_of(room_id), 20)
    for _ in range(4):
        sockets = []
        for _ in range(5):
            ws = await ws_connect(live, room_id, alice.secret)
            await send_resume(ws, 0)
            sockets.append(ws)
        await asyncio.sleep(0.05)
        for ws in sockets:
            await ws.close()
    await wait_until(lambda: live.hub.active_connections == 0)
    await live.hub.manager.wait_idle()
    assert live.hub.rooms == {} and live.hub.replaying == 0
    assert live.hub.manager.reference_counts == {} and live.hub.replay_slots._value == 20
    assert list(await redis_client.pubsub_channels(f"{settings.redis_channel_prefix}:*")) == []
    stray = [
        t.get_name()
        for t in asyncio.all_tasks()
        if t.get_name().startswith(("ws-writer", "ws-receiver", "ws-watchdog", "ws-drainer"))
    ]
    assert stray == []  # no leaked per-connection tasks
    json.dumps(stray)
