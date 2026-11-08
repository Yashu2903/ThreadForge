"""The room WebSocket against a REAL uvicorn server, a real `websockets` client and real Redis.

Cookie and Origin headers are sent explicitly by the client. Outbox rows are published by an
in-process dispatcher (the `pump` fixture) to real Redis. Since Part 5 every connection sends
`resume` first and then reads durable events in order from PostgreSQL; Redis only wakes it up.
Replay, flow control and reconnect behaviour are in test_ws_replay.py / test_ws_flow.py.
"""

import asyncio
import json
import uuid

import pytest
from websockets.exceptions import InvalidStatus

from tests.helpers import add_member, execute, rows
from tests.realtime_helpers import (
    drain_until_closed,
    expect_no_event,
    live_server,
    open_synced,
    recv_event,
    recv_json,
    send_resume,
    wait_until,
    ws_connect,
)
from tests.test_invitations_api import new_room
from tests.test_members_api import member_path
from tests.test_messages_edit_delete import delete, edit
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio


async def expect_rejected(coro):
    """The handshake must fail with a bare HTTP 403 and nothing else."""
    with pytest.raises(InvalidStatus) as caught:
        await coro
    assert caught.value.response.status_code == 403
    assert caught.value.response.body in (b"", None) or b"error" not in caught.value.response.body
    return caught.value.response


# ---- handshake ---------------------------------------------------------------------------------


async def test_member_connects_and_gets_sync_frames_with_current_seq(live, alice):
    room_id = await new_room(alice)
    await send(alice, room_id, "hi")  # seq 2 (room_created = 1)
    ws, frames = await open_synced(live, room_id, alice, after_seq=2)
    assert [f["type"] for f in frames] == ["sync_started", "sync_complete"]
    assert frames[0] == {
        "type": "sync_started",
        "schema_version": 1,
        "room_id": room_id,
        "after_seq": 2,
        "through_seq": 2,
    }
    assert frames[1] == {"type": "sync_complete", "through_seq": 2}
    await ws.close()


async def test_every_handshake_failure_is_the_same_403(live, alice, bob, sessionmaker):
    room_id = await new_room(alice)
    responses = [
        await expect_rejected(ws_connect(live, room_id, None)),  # no cookie
        await expect_rejected(ws_connect(live, room_id, "x" * 43)),  # garbage cookie
        await expect_rejected(ws_connect(live, room_id, bob.secret)),  # not a member
        await expect_rejected(ws_connect(live, uuid.uuid4(), alice.secret)),  # unknown room
        await expect_rejected(
            ws_connect(live, room_id, alice.secret, url=live.ws_url("not-a-uuid"))
        ),
        await expect_rejected(ws_connect(live, room_id, alice.secret, cookie_name="other")),
        await expect_rejected(ws_connect(live, room_id, alice.secret, origin=None)),
    ]
    for origin in (
        "http://evil.example",
        "http://localhost:8000.evil.example",
        "https://localhost:8000",
        "http://localhost:8001",
        "null",
    ):
        responses.append(
            await expect_rejected(ws_connect(live, room_id, alice.secret, origin=origin))
        )
    shapes = {
        (r.status_code, bytes(r.body or b""), r.headers.get("content-type")) for r in responses
    }
    assert len(shapes) == 1, shapes


async def test_expired_revoked_and_removed_are_all_rejected_at_handshake(
    live, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    ws, _ = await open_synced(live, room_id, bob)  # works while valid
    await ws.close()
    await execute(
        sessionmaker, "UPDATE sessions SET revoked_at = now() WHERE user_id = :u", u=bob.id
    )
    await expect_rejected(ws_connect(live, room_id, bob.secret))
    await execute(
        sessionmaker, "UPDATE sessions SET revoked_at = NULL WHERE user_id = :u", u=bob.id
    )
    await execute(
        sessionmaker,
        "UPDATE sessions SET created_at = now() - interval '2 days',"
        " expires_at = now() - interval '1 second' WHERE user_id = :u",
        u=bob.id,
    )
    await expect_rejected(ws_connect(live, room_id, bob.secret))
    await execute(
        sessionmaker,
        "UPDATE sessions SET expires_at = now() + interval '1 day' WHERE user_id = :u",
        u=bob.id,
    )
    await add_member(sessionmaker, room_id, bob.id, "editor", state="removed")
    await expect_rejected(ws_connect(live, room_id, bob.secret))


async def test_credentials_in_the_url_are_never_accepted(live, alice):
    room_id = await new_room(alice)
    url = f"{live.ws_url(room_id)}?session={alice.secret}&tf_session={alice.secret}"
    await expect_rejected(ws_connect(live, room_id, None, url=url))


# ---- delivery: durable events read from PostgreSQL, in order --------------------------------------


async def test_message_events_reach_members_in_seq_order_and_not_other_rooms(
    live, pump, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    other_room = await new_room(alice, "Other")
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    await pump.flush()
    ws_bob, _ = await open_synced(live, room_id, bob, after_seq=1)
    ws_other, _ = await open_synced(live, other_room, alice, after_seq=1)

    msg = (await send(alice, room_id, "first")).json()
    edited = await edit(alice, room_id, msg["id"], "second", msg["version"])
    await delete(alice, room_id, msg["id"], edited.json()["version"])
    await pump.flush()

    frames = [await recv_event(ws_bob) for _ in range(3)]
    assert [f["event_type"] for f in frames] == [
        "message_created",
        "message_edited",
        "message_deleted",
    ]
    assert [f["room_seq"] for f in frames] == [2, 3, 4]
    assert all(f["room_id"] == room_id for f in frames)
    assert "first" not in str(frames) and "second" not in str(frames)  # events carry no text
    await expect_no_event(ws_bob)
    await expect_no_event(ws_other)
    await ws_bob.close()
    await ws_other.close()


async def test_a_new_connection_gets_missed_events_by_replay_without_any_hint(live, alice):
    """No dispatcher runs at all: events are still delivered, because replay reads PostgreSQL."""
    room_id = await new_room(alice)
    await send(alice, room_id, "a")
    await send(alice, room_id, "b")
    ws, frames = await open_synced(live, room_id, alice, after_seq=0)
    assert [f.get("room_seq") for f in frames[1:-1]] == [1, 2, 3]
    assert frames[0]["through_seq"] == frames[-1]["through_seq"] == 3
    await ws.close()


async def test_forwarding_performs_no_database_writes(live, pump, alice, bob, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await pump.flush()
    ws, _ = await open_synced(live, room_id, bob, after_seq=1)
    await send(alice, room_id, "x")
    await pump.flush()
    tables = ["rooms", "memberships", "messages", "room_events", "sessions", "users", "outbox"]

    async def snap():
        return {t: await rows(sessionmaker, f"SELECT * FROM {t} ORDER BY 1") for t in tables}

    event = await recv_event(ws)
    assert event["event_type"] == "message_created"
    before = await snap()
    await expect_no_event(ws, 0.2)
    assert before == await snap()
    await ws.close()


async def test_a_repeated_hint_never_repeats_a_frame(live, pump, alice, redis_client, settings):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    await send(alice, room_id, "dup")
    await pump.flush()
    created = await recv_event(ws)
    channel = f"{settings.redis_channel_prefix}:room:{room_id}"
    for _ in range(3):
        await redis_client.publish(
            channel,
            json.dumps(
                {
                    "schema_version": 1,
                    "event_id": created["event_id"],
                    "room_id": room_id,
                    "room_seq": created["room_seq"],
                }
            ),
        )
    await expect_no_event(ws, 0.4)
    await ws.close()


async def test_bad_or_bogus_hints_are_ignored_and_the_connection_survives(
    live, alice, redis_client, settings
):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    channel = f"{settings.redis_channel_prefix}:room:{room_id}"
    shape = {"schema_version": 1, "event_id": str(uuid.uuid4()), "room_id": room_id, "room_seq": 1}
    for payload in (
        "not json",
        "[]",
        json.dumps({**shape, "extra": 1}),
        json.dumps({**shape, "room_id": str(uuid.uuid4())}),  # channel mismatch
        "x" * 5000,  # oversized
        json.dumps({**shape, "room_seq": 10**12}),  # well formed but far ahead of the database
        json.dumps({**shape, "room_seq": 7}),
    ):
        await redis_client.publish(channel, payload)
    await wait_until(lambda: live.hub.manager.hints_received >= 7)
    await expect_no_event(ws, 0.4)
    assert live.hub.manager.rejected["oversized"] >= 1 and live.hub.manager.rejected["schema"] >= 1
    # the bogus hint is forgotten, so a real event afterwards still flows
    await send(alice, room_id, "real")
    await recv_event(ws)  # delivered by the next wake (hint or reconciliation)
    await ws.close()


# ---- access changes while connected -----------------------------------------------------------


async def test_removed_member_gets_close_4403_and_never_their_own_removal_frame(
    live, pump, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await pump.flush()
    ws_bob, _ = await open_synced(live, room_id, bob, after_seq=1)
    ws_alice, _ = await open_synced(live, room_id, alice, after_seq=1)

    response = await alice.delete(member_path(room_id, bob))
    assert response.status_code in (200, 204), response.text
    await pump.flush()

    frames, code = await drain_until_closed(ws_bob)
    assert code == 4403
    assert [f for f in frames if f["type"] == "room_event"] == []
    removal = await recv_event(ws_alice)  # the owner is told
    assert removal["event_type"] == "member_removed"
    assert removal["payload"]["user_id"] == str(bob.id)
    await ws_alice.close()


async def test_revoked_session_is_closed_4401_on_the_next_event(
    live, pump, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await pump.flush()
    ws_bob, _ = await open_synced(live, room_id, bob, after_seq=1)
    await execute(
        sessionmaker, "UPDATE sessions SET revoked_at = now() WHERE user_id = :u", u=bob.id
    )
    await send(alice, room_id, "poke")
    await pump.flush()
    frames, code = await drain_until_closed(ws_bob)
    assert code == 4401
    assert [f for f in frames if f["type"] == "room_event"] == []


async def test_expired_session_is_closed_4401_on_the_next_event(
    live, pump, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await pump.flush()
    ws_bob, _ = await open_synced(live, room_id, bob, after_seq=1)
    await execute(
        sessionmaker,
        "UPDATE sessions SET created_at = now() - interval '2 days',"
        " expires_at = now() - interval '1 second' WHERE user_id = :u",
        u=bob.id,
    )
    await send(alice, room_id, "poke")
    await pump.flush()
    _, code = await drain_until_closed(ws_bob)
    assert code == 4401


async def test_one_access_query_serves_all_local_connections(
    settings, engine, redis_client, pump, make_actor, monkeypatch
):
    from app.services import realtime_access

    tight = settings.model_copy(update={"access_cache_seconds": 0.5})
    alice = await make_actor("Alice")
    async with live_server(tight, engine) as server:
        room_id = await new_room(alice)
        await pump.flush()
        sockets = [await open_synced(server, room_id, alice, after_seq=1) for _ in range(4)]
        await asyncio.sleep(0.6)  # the setup-time cache entries expire
        calls: list[int] = []
        original = realtime_access.check_access

        async def counting(sm, room, session_ids):
            calls.append(len(session_ids))
            return await original(sm, room, session_ids)

        monkeypatch.setattr(realtime_access, "check_access", counting)
        await send(alice, room_id, "fan")
        await pump.flush()
        for ws, _ in sockets:
            await recv_event(ws)
        assert calls == [1]  # ONE query for the room (four connections, one shared session id)
        for ws, _ in sockets:
            await ws.close()


# ---- what clients may send ---------------------------------------------------------------------


async def test_unknown_or_invalid_text_frames_close_4400(live, alice):
    room_id = await new_room(alice)
    for payload in ('{"type":"ping"}', "hello", '{"type":"ack","through_seq":"x"}'):
        ws, _ = await open_synced(live, room_id, alice, after_seq=1)
        await ws.send(payload)
        _, code = await drain_until_closed(ws)
        assert code == 4400, payload


async def test_first_frame_must_be_resume(live, alice):
    room_id = await new_room(alice)
    ws = await ws_connect(live, room_id, alice.secret)
    await ws.send('{"type":"heartbeat"}')
    _, code = await drain_until_closed(ws)
    assert code == 4400


async def test_binary_frame_closes_1003_and_oversized_closes_1009(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    await ws.send(b"\x00\x01")
    _, code = await drain_until_closed(ws)
    assert code == 1003
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    await ws.send("a" * 4096)
    _, code = await drain_until_closed(ws)
    assert code == 1009


async def test_a_second_resume_closes_4400(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    await send_resume(ws, 0)
    _, code = await drain_until_closed(ws)
    assert code == 4400


async def test_heartbeat_is_acknowledged(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    await ws.send('{"type":"heartbeat"}')
    while True:
        frame = await recv_json(ws)
        if frame["type"] == "heartbeat_ack":
            break
    assert set(frame) == {"type", "server_time"}
    await ws.close()


# ---- caps --------------------------------------------------------------------------------------


async def test_per_user_cap_closes_the_sixth_with_4409(live, alice):
    room_id = await new_room(alice)
    sockets = [await open_synced(live, room_id, alice, after_seq=1) for _ in range(5)]
    sixth = await ws_connect(live, room_id, alice.secret)
    _, code = await drain_until_closed(sixth)
    assert code == 4409
    assert live.hub.per_user[alice.id] == 5
    await sockets[0][0].close()
    await wait_until(lambda: live.hub.per_user[alice.id] == 4)
    again, frames = await open_synced(live, room_id, alice, after_seq=1)  # a slot freed up
    assert frames[-1]["type"] == "sync_complete"
    for ws, _ in sockets[1:]:
        await ws.close()
    await again.close()


async def test_per_process_cap_closes_with_4409(settings, engine, redis_client, make_actor):
    small = settings.model_copy(update={"ws_max_connections_per_process": 2})
    a, b, c = [await make_actor(n) for n in ("A", "B", "C")]
    async with live_server(small, engine) as server:
        room_id = await new_room(a)
        sm = server.app.state.sessionmaker
        await add_member(sm, room_id, b.id)
        await add_member(sm, room_id, c.id)
        w1, _ = await open_synced(server, room_id, a, after_seq=1)
        w2, _ = await open_synced(server, room_id, b, after_seq=1)
        w3 = await ws_connect(server, room_id, c.secret)
        _, code = await drain_until_closed(w3)
        assert code == 4409
        await w1.close()
        await w2.close()


# ---- subscription hygiene ----------------------------------------------------------------------


async def test_many_rapid_connects_and_disconnects_leak_nothing(
    live, alice, redis_client, settings
):
    room_ids = [await new_room(alice, f"R{i}") for i in range(3)]

    async def cycle(room_id, delay):
        ws = await ws_connect(live, room_id, alice.secret)
        await send_resume(ws, 0)
        await asyncio.sleep(delay)
        await ws.close()

    for _ in range(6):  # stay under the per-user cap of 5 at any moment
        await asyncio.gather(
            *[cycle(room_ids[i % 3], 0.01 * (i % 3)) for i in range(5)], return_exceptions=True
        )
    await wait_until(lambda: live.hub.active_connections == 0)
    await live.hub.manager.wait_idle()
    assert live.hub.rooms == {} and not live.hub.per_user and not live.hub._serving
    assert live.hub.manager.subscribed_channels == frozenset()
    assert live.hub.manager.reference_counts == {}
    assert list(await redis_client.pubsub_channels(f"{settings.redis_channel_prefix}:*")) == []
    assert live.hub.replaying == 0


async def test_server_shutdown_closes_open_sockets(settings, engine, redis_client, make_actor):
    """Under uvicorn the server itself closes sockets first with 1012; the hub's own 1001 is
    exercised below. Either way the client sees a close, not a hang."""
    a = await make_actor("A")
    async with live_server(settings, engine) as server:
        room_id = await new_room(a)
        ws, _ = await open_synced(server, room_id, a, after_seq=1)
    _, code = await drain_until_closed(ws)
    assert code in (1001, 1012)


async def test_hub_stop_closes_connections_with_1001(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    await live.hub.stop()
    _, code = await drain_until_closed(ws)
    assert code == 1001
    assert live.hub.active_connections == 0
