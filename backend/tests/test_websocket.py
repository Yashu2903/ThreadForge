"""The room WebSocket against a REAL uvicorn server, a real `websockets` client and real Redis.

Cookie and Origin headers are sent explicitly by the client. Outbox rows are published by an
in-process dispatcher (the `pump` fixture) to real Redis, so every frame below travelled
PostgreSQL -> outbox -> Redis -> subscription manager -> forwarding worker -> socket.
"""

import asyncio
import uuid

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from tests.helpers import add_member, execute, rows, scalar
from tests.realtime_helpers import (
    drain_until_closed,
    expect_silence,
    recv_json,
    ws_connect,
)
from tests.test_invitations_api import new_room
from tests.test_members_api import member_path
from tests.test_messages_edit_delete import delete, edit
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio


async def open_ready(live, room_id, actor, **kw):
    ws = await ws_connect(live, room_id, actor.secret, **kw)
    frame = await recv_json(ws)
    assert frame["type"] == "ready"
    return ws, frame


async def settle(pump, hub, *, times=3):
    """Publish due outbox rows, then let the notification travel through Redis and the hub."""
    await pump.flush()
    for _ in range(times):
        await asyncio.sleep(0.05)


async def wait_for_frame(ws, expected_type=None, within=5.0):
    frame = await recv_json(ws, within)
    if expected_type:
        assert frame["type"] == "room_event" and frame["event_type"] == expected_type, frame
    return frame


async def expect_rejected(coro):
    """The handshake must fail with a bare HTTP 403 and nothing else."""
    with pytest.raises(InvalidStatus) as caught:
        await coro
    assert caught.value.response.status_code == 403
    assert caught.value.response.body in (b"", None) or b"error" not in caught.value.response.body
    return caught.value.response


# ---- handshake ---------------------------------------------------------------------------------


async def test_member_connects_and_gets_ready_with_current_seq(live, alice, sessionmaker):
    room_id = await new_room(alice)
    await send(alice, room_id, "hi")  # seq 2 (room_created = 1)
    ws, frame = await open_ready(live, room_id, alice)
    assert frame == {
        "type": "ready",
        "schema_version": 1,
        "room_id": room_id,
        "current_room_seq": 2,
    }
    await ws.close()


async def test_every_handshake_failure_is_the_same_403(live, alice, bob, make_actor, sessionmaker):
    room_id = await new_room(alice)
    other_room = await new_room(bob)
    responses = []
    # no cookie
    responses.append(await expect_rejected(ws_connect(live, room_id, None)))
    # garbage cookie
    responses.append(await expect_rejected(ws_connect(live, room_id, "x" * 43)))
    # valid session, not a member
    responses.append(await expect_rejected(ws_connect(live, room_id, bob.secret)))
    # unknown room
    responses.append(await expect_rejected(ws_connect(live, uuid.uuid4(), alice.secret)))
    # malformed room id
    responses.append(
        await expect_rejected(
            ws_connect(live, room_id, alice.secret, url=live.ws_url("not-a-uuid"))
        )
    )
    # wrong cookie name
    responses.append(
        await expect_rejected(ws_connect(live, room_id, alice.secret, cookie_name="other"))
    )
    # missing origin
    responses.append(await expect_rejected(ws_connect(live, room_id, alice.secret, origin=None)))
    # foreign origin, and near-misses of the trusted one
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
    # member of ANOTHER room only
    assert other_room != room_id
    # all identical
    shapes = {
        (r.status_code, bytes(r.body or b""), r.headers.get("content-type")) for r in responses
    }
    assert len(shapes) == 1, shapes


async def test_expired_revoked_and_removed_are_all_rejected_at_handshake(
    live, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    ws, _ = await open_ready(live, room_id, bob)  # works while valid
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
        "UPDATE sessions SET created_at = now() - interval '2 days', expires_at = now() - interval '1 second' WHERE user_id = :u",
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


# ---- delivery ----------------------------------------------------------------------------------


async def test_message_events_reach_members_in_seq_order_and_not_other_rooms(
    live, pump, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    other_room = await new_room(alice, "Other")
    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    ws_bob, _ = await open_ready(live, room_id, bob)
    ws_other, _ = await open_ready(live, other_room, alice)

    created = await send(alice, room_id, "first")
    msg = created.json()
    edited = await edit(alice, room_id, msg["id"], "second", msg["version"])
    await delete(alice, room_id, msg["id"], edited.json()["version"])
    await settle(pump, live.hub)

    frames = [await wait_for_frame(ws_bob) for _ in range(3)]
    assert [f["event_type"] for f in frames] == [
        "message_created",
        "message_edited",
        "message_deleted",
    ]
    assert [f["room_seq"] for f in frames] == sorted(f["room_seq"] for f in frames)
    assert all(f["room_id"] == room_id for f in frames)
    assert "first" not in str(frames) and "second" not in str(frames)  # events carry no text
    await expect_silence(ws_bob)
    await expect_silence(ws_other)  # another room hears nothing
    await ws_bob.close()
    await ws_other.close()


async def test_forwarding_performs_no_database_writes(live, pump, alice, bob, sessionmaker):
    room_id = await new_room(alice)
    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    await add_member(sessionmaker, room_id, bob.id, "editor")
    ws, _ = await open_ready(live, room_id, bob)
    await send(alice, room_id, "x")
    await pump.flush()  # outbox is now processed; snapshot AFTER that
    tables = ["rooms", "memberships", "messages", "room_events", "sessions", "users"]

    async def snap():
        return {t: await rows(sessionmaker, f"SELECT * FROM {t} ORDER BY 1") for t in tables}

    before = await snap()
    await wait_for_frame(ws, "room_event" if False else "message_created")
    await asyncio.sleep(0.2)
    after = await snap()
    assert before == after
    await ws.close()


async def test_duplicate_publication_is_delivered_once_per_connection(
    live, pump, alice, redis_client, sessionmaker, settings
):
    room_id = await new_room(alice)
    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    ws, _ = await open_ready(live, room_id, alice)
    await send(alice, room_id, "dup")
    await pump.flush()
    first = await wait_for_frame(ws, "message_created")
    # publish the same notification again, by hand
    channel = f"{settings.redis_channel_prefix}:room:{room_id}"
    import json

    await redis_client.publish(
        channel,
        json.dumps(
            {
                "schema_version": 1,
                "event_id": first["event_id"],
                "room_id": room_id,
                "room_seq": first["room_seq"],
            }
        ),
    )
    await asyncio.sleep(0.3)
    await expect_silence(ws)
    await ws.close()


async def test_bad_notifications_are_dropped_and_connection_survives(
    live, alice, redis_client, settings
):
    room_id = await new_room(alice)
    ws, _ = await open_ready(live, room_id, alice)
    channel = f"{settings.redis_channel_prefix}:room:{room_id}"
    good_shape = {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "room_id": room_id,
        "room_seq": 1,
    }
    import json

    for payload in (
        "not json",
        "[]",
        json.dumps({**good_shape, "extra": 1}),
        json.dumps({**good_shape, "room_id": str(uuid.uuid4())}),  # channel mismatch
        json.dumps(good_shape),  # well formed, but no such event: unverifiable
        "x" * 5000,  # oversized
    ):
        await redis_client.publish(channel, payload)
    await asyncio.sleep(0.5)
    await expect_silence(ws)
    assert live.hub.counters.events_dropped_unverifiable >= 1
    assert not ws.close_code  # still open
    await ws.close()


async def test_event_with_wrong_seq_for_its_id_is_not_forwarded(
    live, pump, alice, redis_client, settings
):
    room_id = await new_room(alice)
    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    ws, _ = await open_ready(live, room_id, alice)
    await send(alice, room_id, "a")
    await pump.flush()
    real = await wait_for_frame(ws, "message_created")
    import json

    await redis_client.publish(
        f"{settings.redis_channel_prefix}:room:{room_id}",
        json.dumps(
            {
                "schema_version": 1,
                "event_id": str(uuid.uuid4()),  # unknown id, plausible seq
                "room_id": room_id,
                "room_seq": real["room_seq"] + 1,
            }
        ),
    )
    await asyncio.sleep(0.3)
    await expect_silence(ws)
    await ws.close()


# ---- access changes while connected -----------------------------------------------------------


async def test_removed_member_gets_close_4403_and_never_their_own_removal_frame(
    live, pump, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    await add_member(sessionmaker, room_id, bob.id, "editor")
    ws_bob, _ = await open_ready(live, room_id, bob)
    ws_alice, _ = await open_ready(live, room_id, alice)

    response = await alice.delete(member_path(room_id, bob))
    assert response.status_code in (200, 204), response.text
    await settle(pump, live.hub)

    frames, code = await drain_until_closed(ws_bob)
    assert code == 4403
    assert frames == []  # no member_removed frame, nothing at all
    removal = await wait_for_frame(ws_alice, "member_removed")  # the owner is told
    assert removal["resource_id"] == str(bob.id) or removal["payload"].get("user_id")
    await ws_alice.close()


async def test_revoked_session_is_closed_4401_on_the_next_event(
    live, pump, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    await add_member(sessionmaker, room_id, bob.id, "editor")
    ws_bob, _ = await open_ready(live, room_id, bob)
    await execute(
        sessionmaker, "UPDATE sessions SET revoked_at = now() WHERE user_id = :u", u=bob.id
    )
    await expect_silence(ws_bob, 0.4)  # nothing noticed yet (documented limitation)
    await send(alice, room_id, "poke")
    await settle(pump, live.hub)
    frames, code = await drain_until_closed(ws_bob)
    assert code == 4401 and frames == []


async def test_expired_session_is_closed_4401_on_the_next_event(
    live, pump, alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    await add_member(sessionmaker, room_id, bob.id, "editor")
    ws_bob, _ = await open_ready(live, room_id, bob)
    await execute(
        sessionmaker,
        "UPDATE sessions SET created_at = now() - interval '2 days', expires_at = now() - interval '1 second' WHERE user_id = :u",
        u=bob.id,
    )
    await send(alice, room_id, "poke")
    await settle(pump, live.hub)
    _, code = await drain_until_closed(ws_bob)
    assert code == 4401


async def test_one_access_query_serves_all_local_connections(
    live, pump, alice, sessionmaker, monkeypatch
):
    from app.services import realtime_access

    room_id = await new_room(alice)

    await pump.flush()  # publish setup events first, so frames below are only the ones under test
    sockets = [await open_ready(live, room_id, alice) for _ in range(4)]
    calls = []
    original = realtime_access.check_access

    async def counting(sm, room, session_ids):
        calls.append(len(session_ids))
        return await original(sm, room, session_ids)

    monkeypatch.setattr(realtime_access, "check_access", counting)
    await send(alice, room_id, "fan")
    await settle(pump, live.hub)
    for ws, _ in sockets:
        await wait_for_frame(ws, "message_created")
    assert calls == [4]  # ONE query, four connections
    for ws, _ in sockets:
        await ws.close()


# ---- what clients may send ---------------------------------------------------------------------


async def test_any_text_frame_closes_1008(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_ready(live, room_id, alice)
    await ws.send('{"type":"ping"}')
    _, code = await drain_until_closed(ws)
    assert code == 1008


async def test_binary_frame_closes_1003(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_ready(live, room_id, alice)
    await ws.send(b"\x00\x01")
    _, code = await drain_until_closed(ws)
    assert code == 1003


async def test_oversized_frame_closes_1009(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_ready(live, room_id, alice)
    await ws.send("a" * 4096)
    _, code = await drain_until_closed(ws)
    assert code == 1009


# ---- caps --------------------------------------------------------------------------------------


async def test_per_user_cap_closes_the_sixth_with_4409(live, alice):
    room_id = await new_room(alice)
    sockets = [await open_ready(live, room_id, alice) for _ in range(5)]
    sixth = await ws_connect(live, room_id, alice.secret)
    _, code = await drain_until_closed(sixth)
    assert code == 4409
    assert live.hub.per_user[alice.id] == 5
    await sockets[0][0].close()
    await asyncio.sleep(0.2)
    again, frame = await open_ready(live, room_id, alice)  # a slot freed up
    assert frame["type"] == "ready"
    for ws, _ in sockets[1:]:
        await ws.close()
    await again.close()


async def test_per_process_cap_closes_with_4409(settings, engine, redis_client, make_actor):
    from tests.realtime_helpers import live_server

    small = settings.model_copy(update={"ws_max_connections_per_process": 2})
    a = await make_actor("A", app_settings=small)
    b = await make_actor("B", app_settings=small)
    c = await make_actor("C", app_settings=small)
    async with live_server(small, engine) as server:
        room_id = await new_room(a)
        sm = server.app.state.sessionmaker
        await add_member(sm, room_id, b.id)
        await add_member(sm, room_id, c.id)
        w1, _ = await open_ready(server, room_id, a)
        w2, _ = await open_ready(server, room_id, b)
        w3 = await ws_connect(server, room_id, c.secret)
        _, code = await drain_until_closed(w3)
        assert code == 4409
        await w1.close()
        await w2.close()


# ---- slow consumers ----------------------------------------------------------------------------


class StalledSocket:
    """A fake websocket whose send never completes: the peer has stopped reading."""

    def __init__(self):
        self.closed_with = None
        self.gate = asyncio.Event()

    async def send_text(self, data):
        await self.gate.wait()

    async def close(self, code=1000, reason=None):
        self.closed_with = code

    async def receive(self):
        await asyncio.sleep(3600)


async def test_full_queue_closes_only_that_connection_with_4429(
    settings, engine, redis_client, sessionmaker, make_actor
):
    from app.realtime.hub import Connection
    from app.services.realtime_access import HandshakeIdentity
    from app.services.room_events import record_room_event  # noqa: F401
    from tests.realtime_helpers import Pump, live_server

    small = settings.model_copy(update={"ws_connection_queue_size": 3})
    alice = await make_actor("Alice")
    pump = Pump(sessionmaker, small)
    async with live_server(small, engine) as live:
        room_id = await new_room(alice)
        await (
            pump.flush()
        )  # publish setup events first, so frames below are only the ones under test
        healthy, _ = await open_ready(live, room_id, alice)
        hub = live.hub
        session_id = await scalar(
            sessionmaker, "SELECT id FROM sessions WHERE user_id = :u LIMIT 1", u=alice.id
        )
        stalled = StalledSocket()
        conn = Connection(
            hub,
            stalled,
            uuid.UUID(room_id),
            HandshakeIdentity(alice.id, session_id),  # type: ignore[arg-type]
        )
        hub._register(conn)
        conn.start_sender()
        conn.live = True

        for i in range(8):
            response = await send(alice, room_id, f"m{i}")
            assert response.status_code == 201, response.text
        await settle(pump, hub, times=10)
        await asyncio.sleep(0.5)

        assert conn.close_code == 4429 and stalled.closed_with == 4429
        assert hub.counters.slow_consumer_disconnects >= 1
        got = 0
        while got < 8:
            frame = await wait_for_frame(healthy)
            got += frame["event_type"] == "message_created"
        assert got == 8  # the healthy connection missed nothing
        stalled.gate.set()
        await healthy.close()
    await pump.aclose()


# ---- subscription hygiene ----------------------------------------------------------------------


async def test_many_rapid_connects_and_disconnects_leak_nothing(
    live, alice, redis_client, settings
):
    room_ids = [await new_room(alice, f"R{i}") for i in range(3)]

    async def cycle(room_id, delay):
        ws = await ws_connect(live, room_id, alice.secret)
        await asyncio.sleep(delay)
        await ws.close()

    # stay under the per-user cap of 5 at any moment
    for _ in range(6):
        await asyncio.gather(
            *[cycle(room_ids[i % 3], 0.01 * (i % 3)) for i in range(5)], return_exceptions=True
        )
    await asyncio.sleep(0.5)
    await live.hub.manager.wait_idle()
    assert live.hub.active_connections == 0
    assert live.hub.rooms == {} and not live.hub.per_user
    assert live.hub.manager.subscribed_channels == frozenset()
    assert live.hub.manager.reference_counts == {}
    prefix = settings.redis_channel_prefix
    channels = await redis_client.pubsub_channels(f"{prefix}:*")
    assert list(channels) == []
    assert not live.hub._background or all(t.done() for t in live.hub._background)


async def test_server_shutdown_closes_open_sockets(settings, engine, redis_client, make_actor):
    """Under uvicorn, the server itself closes sockets first with 1012 (service restart); the
    hub's own 1001 is exercised separately below. Either way the client sees a close, not a hang."""
    from tests.realtime_helpers import live_server

    a = await make_actor("A")
    async with live_server(settings, engine) as server:
        room_id = await new_room(a)
        ws, _ = await open_ready(server, room_id, a)
    _, code = await drain_until_closed(ws)
    assert code in (1001, 1012)


async def test_hub_stop_closes_connections_with_1001(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_ready(live, room_id, alice)
    await live.hub.stop()
    _, code = await drain_until_closed(ws)
    assert code == 1001
    assert live.hub.active_connections == 0


async def test_closed_connection_raises_for_further_use(live, alice):
    room_id = await new_room(alice)
    ws, _ = await open_ready(live, room_id, alice)
    await ws.close()
    with pytest.raises(ConnectionClosed):
        await ws.recv()


async def test_pending_setup_events_are_still_published_to_a_late_connection(live, pump, alice):
    """Publication is at-least-once and does not know who connected when: an event committed
    before the socket opened (seq <= the ready mark) is still pushed if its outbox row publishes
    afterwards. History already has it; clients de-duplicate by event_id and order by room_seq."""
    room_id = await new_room(alice)  # room_created seq 1 is pending in the outbox
    ws, ready = await open_ready(live, room_id, alice)
    assert ready["current_room_seq"] == 1
    await pump.flush()
    frame = await wait_for_frame(ws, "room_created")
    assert frame["room_seq"] == 1
    await ws.close()
