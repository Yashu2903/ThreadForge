"""Live access enforcement: removal, logout, expiry, disabled accounts, lost hints, database outage.

A connection's access is checked at the handshake, before replay, before every page, before typing
and availability, on every room hint (immediately, shared by all of a room's local connections),
and by a periodic sweep over every connection INCLUDING idle ones. Intervals are shrunk here.
"""

import asyncio
import uuid

import httpx2
import pytest
from sqlalchemy.exc import OperationalError

from app.services import realtime_access
from app.services.realtime_access import HandshakeIdentity
from tests.helpers import add_member, append_events, execute, scalar
from tests.realtime_helpers import (
    TRUSTED_ORIGIN,
    FakeSocket,
    LiveServer,
    drain_until_closed,
    live_server,
    open_synced,
    recv_event,
    send_json,
    wait_until,
    ws_connect,
)
from tests.test_invitations_api import new_room
from tests.test_members_api import member_path
from tests.test_messages_send import send
from tests.test_realtime_failures import free_port, start_api, stop, wait_ready

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fast(settings):
    return settings.model_copy(
        update={
            "access_check_interval_seconds": 0.5,
            "access_cache_seconds": 0.05,
            "reconcile_interval_seconds": 0.4,
            "reconcile_interval_degraded_seconds": 0.2,
            "presence_tick_seconds": 0.5,
        }
    )


async def _entries_is(redis_client, key, expected):
    return await redis_client.zcard(key) == expected


def revoke(sessionmaker, user_id):
    return execute(
        sessionmaker, "UPDATE sessions SET revoked_at = now() WHERE user_id = :u", u=user_id
    )


# ---- removal --------------------------------------------------------------------------------------------


async def test_removal_closes_the_socket_within_a_moment_through_the_hint(
    fast, engine, redis_client, pump, alice, bob, sessionmaker
):
    slow_sweep = fast.model_copy(
        update={"access_check_interval_seconds": 60.0}
    )  # the sweep is NOT it
    async with live_server(slow_sweep, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id, "editor")
        await pump.flush()
        ws, _ = await open_synced(server, room_id, bob, after_seq=1)
        assert (await alice.delete(member_path(room_id, bob))).status_code in (200, 204)
        started = asyncio.get_running_loop().time()
        await pump.flush()
        frames, code = await drain_until_closed(ws, within=5)
        assert code == 4403 and [f for f in frames if f["type"] == "room_event"] == []
        assert asyncio.get_running_loop().time() - started < 2  # a moment, not an interval


async def test_removal_is_enforced_by_the_sweep_when_every_hint_is_lost(
    fast, engine, redis_client, alice, bob, sessionmaker
):
    async with live_server(fast, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id, "editor")
        ws, _ = await open_synced(server, room_id, bob, after_seq=1)
        server.hub.suppress_hints = True  # no hint will ever arrive; no dispatcher runs either
        assert (await alice.delete(member_path(room_id, bob))).status_code in (200, 204)
        _, code = await drain_until_closed(ws, within=5)  # the periodic check finds it
        assert code == 4403
        assert server.hub.counters.access_periodic_runs >= 1


async def test_removal_closes_the_connection_across_api_processes(
    alice, bob, sessionmaker, pump, redis_prefix
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await pump.flush()
    ports = [free_port(), free_port()]
    procs = [start_api(p, redis_prefix) for p in ports]
    try:
        for port in ports:
            await wait_ready(port)
        other = LiveServer(None, ports[1], None, None)  # type: ignore[arg-type]
        ws, _ = await open_synced(other, room_id, bob, after_seq=1)
        # the removal is performed through the FIRST process, over real HTTP
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{ports[0]}") as client:
            client.cookies.set("tf_session", alice.secret)
            csrf = (await client.get("/auth/csrf")).json()["csrf_token"]
            response = await client.delete(
                f"/rooms/{room_id}/members/{bob.id}",
                headers={"Origin": TRUSTED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert response.status_code in (200, 204), response.text
        await pump.flush()
        frames, code = await drain_until_closed(ws, within=10)
        assert code == 4403 and [f for f in frames if f["type"] == "room_event"] == []
    finally:
        for p in procs:
            stop(p)


# ---- logout, expiry, disabled accounts: no room event exists, the sweep bounds them --------------------


@pytest.mark.parametrize("how", ["logout", "expired", "disabled"])
async def test_logout_expiry_and_disabled_accounts_close_4401_within_the_check_interval(
    fast, engine, redis_client, alice, bob, sessionmaker, how
):
    async with live_server(fast, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id, "editor")
        ws, _ = await open_synced(server, room_id, bob, after_seq=1)
        started = asyncio.get_running_loop().time()
        if how == "logout":
            await revoke(sessionmaker, bob.id)
        elif how == "expired":
            await execute(
                sessionmaker,
                "UPDATE sessions SET created_at = now() - interval '2 days',"
                " expires_at = now() - interval '1 second' WHERE user_id = :u",
                u=bob.id,
            )
        else:
            await execute(
                sessionmaker, "UPDATE users SET state = 'disabled' WHERE id = :u", u=bob.id
            )
        _, code = await drain_until_closed(ws, within=5)  # an IDLE socket: no event ever came
        assert code == 4401
        assert (
            asyncio.get_running_loop().time() - started < 2.5
        )  # <= interval (0.5 s) + jitter + slack
        # and the reconnect is refused outright
        with pytest.raises(Exception) as refused:  # noqa: PT011 - InvalidStatus
            await ws_connect(server, room_id, bob.secret)
        assert "403" in str(refused.value)


# ---- what happens to a revoked connection -----------------------------------------------------------------


async def test_pending_frames_are_discarded_and_nothing_protected_is_sent_after_revocation(
    fast, engine, redis_client, alice, bob, sessionmaker
):
    async with live_server(fast, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id, "editor")
        await append_events(sessionmaker, uuid.UUID(room_id), 40)
        sock = FakeSocket()
        sock.gate.clear()  # the peer's TCP window is full: sends block, frames pile up
        session_id = await scalar(
            sessionmaker, "SELECT id FROM sessions WHERE user_id = :u", u=bob.id
        )
        sock.feed(type="resume", schema_version=1, after_seq=0)
        serve = asyncio.create_task(
            server.hub.serve(sock, uuid.UUID(room_id), HandshakeIdentity(bob.id, session_id))
        )
        conn = None

        async def queued():
            nonlocal conn
            for state in server.hub.rooms.values():
                for c in state.connections.values():
                    conn = c
                    return len(c.outbox) >= 5
            return False

        await wait_until(queued)
        await revoke(sessionmaker, bob.id)
        await server.hub.access_sweep()  # what the periodic check does
        assert conn.closing and conn.close_code == 4401
        assert len(conn.outbox) <= 1  # the pending frames were thrown away (at most the final one)
        sock.gate.set()
        await asyncio.wait_for(serve, timeout=5)
        events_sent = [f for f in sock.sent if f["type"] == "room_event"]
        assert events_sent == []  # only the frame already in flight (sync_started) went out
        assert sock.closed_with == 4401


async def test_a_closed_connection_gives_back_everything_it_held(
    fast, engine, redis_client, pump, alice, bob, sessionmaker, redis_prefix
):
    async with live_server(fast, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id, "editor")
        await pump.flush()
        a, _ = await open_synced(server, room_id, alice, after_seq=1)
        b, _ = await open_synced(server, room_id, bob, after_seq=1)
        await send_json(b, type="typing", state="start")
        key = server.hub.presence.store.conns_key(uuid.UUID(room_id))

        async def entries():
            return await redis_client.zcard(key)

        await wait_until(lambda: _entries_is(redis_client, key, 2), 5)
        assert await entries() == 2
        assert server.hub.manager.reference_counts == {uuid.UUID(room_id): 2}
        await alice.delete(member_path(room_id, bob))
        await pump.flush()
        _, code = await drain_until_closed(b, within=5)
        assert code == 4403

        async def settled():
            return (
                await entries() == 1
                and server.hub.manager.reference_counts == {uuid.UUID(room_id): 1}
                and server.hub.per_user[bob.id] == 0
            )

        await wait_until(settled, 5)
        await a.close()


async def test_typing_from_a_revoked_session_closes_4401_and_is_not_applied(
    fast, engine, redis_client, alice, bob, sessionmaker
):
    async with live_server(fast, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id, "viewer")
        ws, _ = await open_synced(server, room_id, bob, after_seq=1)
        await revoke(sessionmaker, bob.id)
        await asyncio.sleep(0.1)  # the access cache (50 ms here) has expired
        await send_json(ws, type="typing", state="start")
        _, code = await drain_until_closed(ws, within=5)
        assert code == 4401
        assert await server.hub.presence.store.typing_snapshot(uuid.UUID(room_id)) == []


# ---- PostgreSQL unavailable -------------------------------------------------------------------------------


def broken(*_args, **_kwargs):
    raise OperationalError("SELECT 1", {}, Exception("database is down"))


async def test_database_outage_sends_nothing_then_closes_1013_after_the_grace_period(
    fast, engine, redis_client, alice, bob, sessionmaker, monkeypatch
):
    grace = fast.model_copy(update={"access_db_grace_seconds": 1.0})
    async with live_server(grace, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id, "editor")
        ws, _ = await open_synced(server, room_id, bob, after_seq=1)
        for name in ("check_access", "fetch_events_page", "read_event_seqs"):

            async def down(*a, **k):
                broken()

            monkeypatch.setattr(realtime_access, name, down)
        await send(alice, room_id, "committed while the websocket layer cannot reach PostgreSQL")
        started = asyncio.get_running_loop().time()
        frames, code = await drain_until_closed(ws, within=8)
        elapsed = asyncio.get_running_loop().time() - started
        assert [f for f in frames if f["type"] == "room_event"] == []  # nothing protected sent
        assert code == 1013 and 0.8 <= elapsed < 4  # after the grace period, not before
        assert server.hub.counters.database_grace_closes >= 1


async def test_a_short_database_outage_is_survived_without_losing_the_event(
    fast, engine, redis_client, pump, alice, bob, sessionmaker, monkeypatch
):
    grace = fast.model_copy(update={"access_db_grace_seconds": 10.0})
    async with live_server(grace, engine) as server:
        room_id = await new_room(alice)
        await pump.flush()
        ws, _ = await open_synced(server, room_id, alice, after_seq=1)
        state = {"down": True}
        originals = {n: getattr(realtime_access, n) for n in ("check_access", "fetch_events_page")}

        def flaky(name):
            async def call(*a, **k):
                if state["down"]:
                    broken()
                return await originals[name](*a, **k)

            return call

        for name in originals:
            monkeypatch.setattr(realtime_access, name, flaky(name))
        await send(alice, room_id, "delivered after the outage")
        await pump.flush()
        await asyncio.sleep(0.6)
        assert not ws.close_code  # still open, nothing sent
        state["down"] = False  # PostgreSQL is back, well within the grace period
        event = await recv_event(ws, 5)
        assert event["event_type"] == "message_created"
        assert server.hub.counters.database_grace_closes == 0
        await ws.close()


async def test_a_hung_database_is_a_failure_too_not_a_silent_stall(
    fast, engine, redis_client, alice, bob, sessionmaker, monkeypatch
):
    """Not an error and not an answer: the query just never returns. Without a per-call timeout the
    connection would hang past the grace period; with one it is closed 1013 like any outage."""
    grace = fast.model_copy(update={"access_db_grace_seconds": 1.5})
    async with live_server(grace, engine) as server:
        room_id = await new_room(alice)
        await add_member(sessionmaker, room_id, bob.id, "editor")
        ws, _ = await open_synced(server, room_id, bob, after_seq=1)

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        for name in ("check_access", "fetch_events_page", "read_event_seqs"):
            monkeypatch.setattr(realtime_access, name, hang)
        await send(alice, room_id, "the database stops answering")
        started = asyncio.get_running_loop().time()
        frames, code = await drain_until_closed(ws, within=10)
        assert code == 1013 and [f for f in frames if f["type"] == "room_event"] == []
        assert asyncio.get_running_loop().time() - started < 6
