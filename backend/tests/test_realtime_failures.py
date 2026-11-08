"""Failure behaviour and multi-process distribution.

What these prove (and do not): HTTP keeps working and keeps recording events when Redis is
unreachable; open sockets survive a lost Redis connection but may miss notifications; two REAL API
processes both deliver a room event published once. None of it proves delivery to a browser.
"""

import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import httpx2
import pytest

from tests.factories import (
    TEST_CLIENT_ID,
    TEST_CLIENT_SECRET,
    TEST_CSRF_SECRET,
    TEST_DATABASE_URL,
    TEST_FINGERPRINT_SECRET,
    TEST_REDIS_URL,
    make_settings,
)
from tests.helpers import add_member, rows, scalar
from tests.realtime_helpers import (
    TRUSTED_ORIGIN,
    LiveServer,
    expect_no_event,
    live_server,
    open_synced,
    recv_event,
    wait_until,
)
from tests.test_invitations_api import new_room
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio

BACKEND_DIR = Path(__file__).resolve().parent.parent
UNREACHABLE_REDIS = "redis://127.0.0.1:1/15"  # nothing listens on port 1


# ---- Redis unreachable --------------------------------------------------------------------------


async def test_http_writes_keep_committing_events_and_pending_outbox_rows_without_redis(
    engine, sessionmaker, make_actor
):
    broken = make_settings(redis_url=UNREACHABLE_REDIS, redis_socket_timeout_seconds=0.3)
    alice = await make_actor("Alice", app_settings=broken)
    async with live_server(broken, engine) as server:
        base = server.http_url
        async with httpx2.AsyncClient(base_url=base) as client:
            client.cookies.set(broken.session_cookie_name, alice.secret)
            csrf = (await client.get("/auth/csrf")).json()["csrf_token"]
            headers = {"Origin": TRUSTED_ORIGIN, "X-CSRF-Token": csrf}
            created = await client.post("/rooms", json={"name": "NoRedis"}, headers=headers)
            assert created.status_code == 201
            room_id = created.json()["id"]
            sent = await client.post(
                f"/rooms/{room_id}/messages",
                json={"client_message_id": str(uuid.uuid4()), "body": "hi"},
                headers=headers,
            )
            assert sent.status_code == 201
            assert (await client.get(f"/rooms/{room_id}/messages")).status_code == 200
            assert (await client.get(f"/rooms/{room_id}/events")).json()["current_room_seq"] == 2
            ready = await client.get("/health/ready")
            assert ready.status_code == 200  # readiness is database-only
    states = await rows(sessionmaker, "SELECT state, attempts FROM outbox ORDER BY room_seq")
    assert states == [{"state": "pending", "attempts": 0}] * 2  # durable, waiting for a dispatcher


async def test_websocket_stays_open_in_degraded_mode_without_redis_and_still_gets_events(
    engine, make_actor
):
    """Redis down: the subscription cannot be established, so the connection is DEGRADED (it
    never closes 1013 for that). Durable delivery continues through reconciliation, which reads
    PostgreSQL on a short interval. No dispatcher runs at all here: it is not needed."""
    broken = make_settings(
        redis_url=UNREACHABLE_REDIS,
        redis_socket_timeout_seconds=0.3,
        ws_subscribe_timeout_seconds=1.0,
        reconcile_interval_seconds=0.3,
        reconcile_interval_degraded_seconds=0.2,
    )
    alice = await make_actor("Alice", app_settings=broken)
    async with live_server(broken, engine) as server:
        room_id = await new_room(alice)  # in-process ASGI client: no Redis involved
        ws, frames = await open_synced(server, room_id, alice, after_seq=1)
        assert frames[-1]["type"] == "sync_complete"  # synchronized despite no Redis
        conn = next(iter(server.hub.rooms[uuid.UUID(room_id)].connections.values()))
        assert conn.degraded is True and server.hub.manager.healthy is False
        await send(alice, room_id, "no redis anywhere")
        event = await recv_event(ws, 5)  # found by reconciliation, not by a hint
        assert event["event_type"] == "message_created" and event["room_seq"] == 2
        assert server.hub.counters.reconcile_wakes >= 1 and server.hub.counters.hints_applied == 0
        await ws.close()


# ---- an open socket survives losing its Redis connection ------------------------------------------


async def kill_pubsub_clients(redis_client):
    """Close our dispatcher-side pub/sub connections (database 15 only) from the Redis server."""
    killed = 0
    for info in await redis_client.client_list(_type="pubsub"):
        if str(info.get("db")) == "15":
            await redis_client.client_kill_filter(_id=info["id"])
            killed += 1
    return killed


async def test_socket_stays_open_across_a_redis_connection_loss_and_resumes(
    live, pump, alice, redis_client, caplog
):
    caplog.set_level(logging.INFO, logger="threadforge")
    room_id = await new_room(alice)
    await pump.flush()
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    assert await kill_pubsub_clients(redis_client) >= 1
    await wait_until(
        lambda: live.hub.manager.status()["connection_losses"] >= 1 and live.hub.manager.healthy,
        10,
    )
    assert any("notification_gap_possible" in r.getMessage() for r in caplog.records)
    assert not ws.close_code  # the socket was never closed
    await send(alice, room_id, "after reconnect")
    await pump.flush()
    frame = await recv_event(ws)
    assert frame["event_type"] == "message_created"  # live delivery resumed
    await ws.close()


async def test_a_lost_hint_costs_at_most_one_reconcile_interval_not_the_event(live, alice, pump):
    """Hints dropped on the floor (test-only fault injection) never lose the event: the next
    reconciliation tick reads PostgreSQL and wakes the connection."""
    live.hub.suppress_hints = True
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    await send(alice, room_id, "hint lost")
    await pump.flush()  # published, but the hub drops the hint
    event = await recv_event(ws, 5)
    assert event["event_type"] == "message_created"
    assert live.hub.counters.hints_applied == 0 and live.hub.counters.reconcile_wakes >= 1
    await ws.close()


async def test_a_late_socket_replays_what_it_missed_even_though_its_hint_is_long_gone(
    live, pump, alice, redis_client, redis_prefix
):
    """Hints are not storage: published while nobody was subscribed, a hint is gone for good.
    The event itself is durable, and a socket that connects (or reconnects) later gets it by
    replay from PostgreSQL."""
    room_id = await new_room(alice)
    await send(alice, room_id, "while nobody listens")
    channel = f"{redis_prefix}:room:{room_id}"
    assert (await redis_client.pubsub_numsub(channel))[0][1] == 0
    assert await pump.flush() == 2  # both published "successfully"; Redis accepted them
    ws, frames = await open_synced(live, room_id, alice, after_seq=0)
    assert [f.get("room_seq") for f in frames[1:-1]] == [1, 2]
    await ws.close()


# ---- hints use constant memory --------------------------------------------------------------------


async def test_ten_thousand_hints_leave_constant_state(live, alice):
    """The Part 4 notification queue (and its drop-oldest policy) is gone: a hint is an
    assignment and an Event.set(), so a flood cannot pile up."""
    room_id = await new_room(alice)
    ws, _ = await open_synced(live, room_id, alice, after_seq=1)
    room = uuid.UUID(room_id)
    for seq in range(1, 10_001):
        live.hub.on_room_hint(room, seq)
    state = live.hub.rooms[room]
    assert state.hint_seq == 10_000 and len(live.hub.rooms) == 1
    await expect_no_event(ws, 0.3)  # a bogus hint far ahead of the database sends nothing
    await send(alice, room_id, "still works")
    event = await recv_event(ws)
    assert event["room_seq"] == 2
    await ws.close()


# ---- two real API processes -----------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api_env(redis_prefix: str) -> dict[str, str]:
    return {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "GOOGLE_CLIENT_ID": TEST_CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": TEST_CLIENT_SECRET,
        "PUBLIC_BASE_URL": TRUSTED_ORIGIN,
        "CSRF_SECRET": TEST_CSRF_SECRET,
        "MESSAGE_FINGERPRINT_SECRET": TEST_FINGERPRINT_SECRET,
        "REDIS_URL": TEST_REDIS_URL,
        "REDIS_CHANNEL_PREFIX": redis_prefix,
    }


def start_api(port: int, redis_prefix: str) -> subprocess.Popen:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(  # noqa: S603 - fixed argv, test-only
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--no-proxy-headers",
            "--ws-max-size",
            "1024",
            "--ws-ping-interval",
            "20",
            "--ws-ping-timeout",
            "20",
        ],  # fmt: skip
        cwd=BACKEND_DIR,
        env=api_env(redis_prefix),
        creationflags=flags,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


async def wait_ready(port: int) -> None:
    async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        for _ in range(150):
            try:
                if (await client.get("/health/ready")).status_code == 200:
                    return
            except httpx2.TransportError:
                pass
            await asyncio.sleep(0.2)
    pytest.fail(f"API process on port {port} never became ready")


def stop(proc: subprocess.Popen) -> str:
    proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
    try:
        output, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
        pytest.fail("an API process did not stop gracefully within 20 s")
    return output


async def test_an_event_published_once_reaches_sockets_on_two_api_processes(
    alice, bob, carol, sessionmaker, pump, redis_prefix
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    await add_member(sessionmaker, room_id, carol.id, "viewer")
    await pump.flush()
    ports = [free_port(), free_port()]
    procs = [start_api(p, redis_prefix) for p in ports]
    outputs: list[str] = []
    try:
        for port in ports:
            await wait_ready(port)
        servers = [LiveServer(None, p, None, None) for p in ports]  # type: ignore[arg-type]
        ws_bob, _ = await open_synced(servers[0], room_id, bob, after_seq=1)
        ws_carol, _ = await open_synced(servers[1], room_id, carol, after_seq=1)

        # Write through the SECOND process over real HTTP; publish once.
        async with httpx2.AsyncClient(base_url=servers[1].http_url) as client:
            client.cookies.set("tf_session", alice.secret)
            csrf = (await client.get("/auth/csrf")).json()["csrf_token"]
            response = await client.post(
                f"/rooms/{room_id}/messages",
                json={"client_message_id": str(uuid.uuid4()), "body": "cross-process"},
                headers={"Origin": TRUSTED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert response.status_code == 201, response.text
        assert await pump.flush() == 1  # ONE publication

        frames = [await recv_event(ws_bob), await recv_event(ws_carol)]
        assert {f["event_id"] for f in frames} == {frames[0]["event_id"]}  # the same event
        assert all(f["event_type"] == "message_created" for f in frames)
        await expect_no_event(ws_bob, 0.4)
        await expect_no_event(ws_carol, 0.4)
        await ws_bob.close()
        await ws_carol.close()
    finally:
        outputs = [stop(p) for p in procs]
    for output in outputs:
        assert "cross-process" not in output  # message text never reaches logs
        assert "tf_session" not in output


# ---- fan-out is not a receipt ---------------------------------------------------------------------


async def test_publish_success_does_not_depend_on_any_subscriber(pump, alice, sessionmaker):
    """A successful publish (outbox row -> processed) says Redis accepted it; nobody need be
    listening. This is why processed != delivered."""
    await new_room(alice)
    assert await pump.flush() == 1
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE state = 'processed'") == 1
