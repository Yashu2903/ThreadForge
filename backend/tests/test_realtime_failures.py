"""Failure behaviour and multi-process distribution.

What these prove (and do not): HTTP keeps working and keeps recording events when Redis is
unreachable; open sockets survive a lost Redis connection but may miss notifications; two REAL API
processes both deliver a room event published once. None of it proves delivery to a browser.
"""

import asyncio
import json
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

from app.realtime.hub import RealtimeHub
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
    expect_silence,
    live_server,
    recv_json,
    ws_connect,
)
from tests.test_invitations_api import new_room
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio

BACKEND_DIR = Path(__file__).resolve().parent.parent
UNREACHABLE_REDIS = "redis://127.0.0.1:1/15"  # nothing listens on port 1


async def open_ready(server, room_id, actor):
    ws = await ws_connect(server, room_id, actor.secret)
    frame = await recv_json(ws)
    assert frame["type"] == "ready"
    return ws, frame


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


async def test_websocket_cannot_go_live_without_redis_and_closes_1013(engine, make_actor):
    broken = make_settings(
        redis_url=UNREACHABLE_REDIS,
        redis_socket_timeout_seconds=0.3,
        ws_subscribe_timeout_seconds=1.0,
    )
    alice = await make_actor("Alice", app_settings=broken)
    async with live_server(broken, engine) as server:
        room_id = await new_room(alice)  # in-process ASGI client: no Redis involved
        ws = await ws_connect(server, room_id, alice.secret)  # the handshake itself succeeds
        with pytest.raises(Exception) as closed:  # noqa: PT011 - ConnectionClosed
            while True:
                await asyncio.wait_for(ws.recv(), timeout=8)
        assert closed.value.rcvd.code == 1013  # try again later, and NO ready frame was sent
        assert server.hub.counters.subscribe_failures == 1
        assert server.hub.active_connections == 0 and server.hub.manager.reference_counts == {}


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
    ws, _ = await open_ready(live, room_id, alice)
    assert await kill_pubsub_clients(redis_client) >= 1
    for _ in range(100):  # the manager notices, reconnects, resubscribes
        status = live.hub.manager.status()
        if status["connection_losses"] >= 1 and status["connected"]:
            break
        await asyncio.sleep(0.1)
    status = live.hub.manager.status()
    assert status["connection_losses"] >= 1 and status["connected"]
    assert any("notification_gap_possible" in r.getMessage() for r in caplog.records)
    assert not ws.close_code  # the socket was never closed
    await send(alice, room_id, "after reconnect")
    await pump.flush()
    frame = await recv_json(ws)
    assert frame["event_type"] == "message_created"  # live delivery resumed
    await ws.close()


async def test_a_notification_with_no_listener_is_gone_but_the_event_is_in_history(
    live, pump, alice, redis_client, redis_prefix
):
    """Notifications are hints, not storage: published while nobody was subscribed, a hint is
    gone for good (Redis Pub/Sub keeps nothing). The event itself is durable, so a socket that
    connects later sees it in history and a current_room_seq that already covers it. Replaying it
    automatically is Part 5."""
    room_id = await new_room(alice)
    await send(alice, room_id, "while nobody listens")
    channel = f"{redis_prefix}:room:{room_id}"
    assert (await redis_client.pubsub_numsub(channel))[0][1] == 0
    assert await pump.flush() == 2  # both published "successfully"; Redis accepted them
    ws, ready = await open_ready(live, room_id, alice)
    assert ready["current_room_seq"] == 2
    await expect_silence(ws)  # nothing is replayed to the socket
    history = (await alice.get(f"/rooms/{room_id}/events")).json()
    assert [e["room_seq"] for e in history["items"]] == [1, 2]
    await ws.close()


# ---- bounded reader queue -------------------------------------------------------------------------


async def test_reader_queue_overflow_drops_the_oldest_and_counts_it(
    settings, sessionmaker, redis_client, redis_prefix
):
    small = settings.model_copy(update={"realtime_queue_size": 2, "ws_forward_workers": 0})
    hub = RealtimeHub(small, sessionmaker)
    await hub.start()
    room = uuid.uuid4()
    try:
        await hub.manager.acquire(room)
        for seq in range(1, 11):
            await redis_client.publish(
                f"{redis_prefix}:room:{room}",
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_id": str(uuid.uuid4()),
                        "room_id": str(room),
                        "room_seq": seq,
                    }
                ),
            )
        for _ in range(100):
            if hub.manager.status()["dropped_notifications"] >= 8:
                break
            await asyncio.sleep(0.05)
        assert hub.manager.status()["dropped_notifications"] == 8
        kept = [hub.notifications.get_nowait().room_seq for _ in range(hub.notifications.qsize())]
        assert kept == [9, 10]  # the NEWEST survive
    finally:
        await hub.stop()


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
        ws_bob, _ = await open_ready(servers[0], room_id, bob)
        ws_carol, _ = await open_ready(servers[1], room_id, carol)

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

        frames = [await recv_json(ws_bob), await recv_json(ws_carol)]
        assert {f["event_id"] for f in frames} == {frames[0]["event_id"]}  # the same event
        assert all(f["event_type"] == "message_created" for f in frames)
        await expect_silence(ws_bob, 0.4)
        await expect_silence(ws_carol, 0.4)
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
