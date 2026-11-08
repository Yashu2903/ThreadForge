"""Helpers for real-WebSocket tests.

The API runs under a REAL uvicorn server (in this process, on an ephemeral port, with the real
lifespan so the hub starts and stops exactly as in production) and tests talk to it with the real
`websockets` client library, sending Cookie and Origin headers explicitly. Outbox rows are
published to the REAL Redis by an in-process dispatcher (`Pump`), never mocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.config import Settings
from app.main import create_app
from app.realtime.publisher import RedisPublisher
from app.workers.outbox_dispatcher import OutboxDispatcher

TRUSTED_ORIGIN = "http://localhost:8000"


@dataclass
class LiveServer:
    app: Any
    port: int
    server: uvicorn.Server
    task: asyncio.Task

    @property
    def hub(self):
        return self.app.state.realtime

    def ws_url(self, room_id) -> str:
        return f"ws://127.0.0.1:{self.port}/rooms/{room_id}/ws"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@contextlib.asynccontextmanager
async def live_server(
    settings: Settings,
    engine,
    *,
    ws_max_size: int = 1024,
    ws_ping_interval: float = 20.0,
    ws_ping_timeout: float = 20.0,
) -> AsyncIterator[LiveServer]:
    app = create_app(settings, engine=engine)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
        ws_max_size=ws_max_size,  # operators should run uvicorn with --ws-max-size 1024
        ws_ping_interval=ws_ping_interval,
        ws_ping_timeout=ws_ping_timeout,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "the live test server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    live = LiveServer(app, port, server, task)
    try:
        yield live
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=15)


async def ws_connect(
    server: LiveServer,
    room_id,
    secret: str | None,
    *,
    origin: str | None = TRUSTED_ORIGIN,
    cookie_name: str = "tf_session",
    extra_headers: dict[str, str] | None = None,
    url: str | None = None,
) -> ClientConnection:
    headers: dict[str, str] = dict(extra_headers or {})
    if secret is not None:
        headers["Cookie"] = f"{cookie_name}={secret}"
    if origin is not None:
        headers["Origin"] = origin
    return await connect(
        url or server.ws_url(room_id),
        additional_headers=headers,
        origin=None,  # we set the Origin header ourselves above (or deliberately omit it)
        open_timeout=10,
        max_size=None,
        max_queue=None,  # never let the client library stop reading (it would stall close())
        ping_interval=None,
    )


async def recv_json(ws: ClientConnection, within: float = 5.0) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=within))


async def expect_silence(ws: ClientConnection, seconds: float = 0.6) -> None:
    """Nothing must arrive (and the socket must stay open) for `seconds`."""
    try:
        frame = await asyncio.wait_for(ws.recv(), timeout=seconds)
    except TimeoutError:
        return
    raise AssertionError(f"unexpected frame: {frame!r}")


async def drain_until_closed(
    ws: ClientConnection, within: float = 8.0
) -> tuple[list[dict], int | None]:
    """Collect every frame until the server closes; returns (frames, close code)."""
    frames: list[dict] = []
    try:
        while True:
            frames.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=within)))
    except ConnectionClosed as exc:
        return frames, (exc.rcvd.code if exc.rcvd else None)


NON_EVENT_FRAMES = {"heartbeat_ack", "presence_snapshot", "typing_state"}


async def send_json(ws: ClientConnection, **frame: Any) -> None:
    await ws.send(json.dumps(frame))


async def send_resume(ws: ClientConnection, after_seq: int = 0) -> None:
    await send_json(ws, type="resume", schema_version=1, after_seq=after_seq)


async def read_until(ws: ClientConnection, frame_type: str, within: float = 8.0) -> list[dict]:
    """Every frame up to and including the first of `frame_type`."""
    frames: list[dict] = []
    while True:
        frame = await recv_json(ws, within)
        frames.append(frame)
        if frame["type"] == frame_type:
            return frames


async def open_synced(
    server: LiveServer, room_id, actor, *, after_seq: int = 0, **kw
) -> tuple[ClientConnection, list[dict]]:
    """Connect, send resume, and read through sync_complete. Returns (socket, sync frames), where
    the frames are sync_started, any replayed room_events, and sync_complete."""
    ws = await ws_connect(server, room_id, actor.secret, **kw)
    await send_resume(ws, after_seq)
    return ws, await read_until(ws, "sync_complete")


async def read_sync_acking(
    ws: ClientConnection, *, ack_every: int = 60, within: float = 8.0
) -> list[dict]:
    """Read through sync_complete, acknowledging every `ack_every` events like a real client
    (the server pauses at ACK_WINDOW unacknowledged events)."""
    frames: list[dict] = []
    since_ack = 0
    while True:
        frame = await recv_json(ws, within)
        frames.append(frame)
        if frame["type"] == "room_event":
            since_ack += 1
            if since_ack >= ack_every:
                await send_json(ws, type="ack", through_seq=frame["room_seq"])
                since_ack = 0
        if frame["type"] == "sync_complete":
            last = [f for f in frames if f["type"] == "room_event"]
            if last:
                await send_json(ws, type="ack", through_seq=last[-1]["room_seq"])
            return frames


async def recv_event(ws: ClientConnection, within: float = 5.0) -> dict[str, Any]:
    """The next room_event, skipping heartbeat acks, presence and typing frames."""
    deadline = asyncio.get_running_loop().time() + within
    while True:
        frame = await recv_json(ws, max(deadline - asyncio.get_running_loop().time(), 0.01))
        if frame["type"] not in NON_EVENT_FRAMES:
            return frame


async def expect_no_event(ws: ClientConnection, seconds: float = 0.6) -> None:
    """No room_event (or anything but presence/typing/heartbeat chatter) for `seconds`."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            return
        if json.loads(frame)["type"] not in NON_EVENT_FRAMES:
            raise AssertionError(f"unexpected frame: {frame!r}")


async def wait_until(condition, within: float = 5.0, interval: float = 0.02, message: str = ""):
    """Poll `condition` (sync or async callable) until truthy; fail after `timeout`. Used instead
    of fixed sleeps."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if loop.time() > deadline:
            raise AssertionError(f"condition not met within {within}s {message}")
        await asyncio.sleep(interval)


class FakeSocket:
    """An in-process stand-in for a Starlette WebSocket, for tests that need a peer the real
    client library cannot be (a stalled reader, exact frame inspection). The peer feeds frames
    with feed(); frames the server sends are recorded; `gate` blocks sends while cleared."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed_with: int | None = None
        self.gate = asyncio.Event()
        self.gate.set()

    def feed(self, **frame: Any) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})

    def feed_raw(self, text: str) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": text})

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def send_text(self, data: str) -> None:
        await self.gate.wait()
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = code

    def types(self) -> list[str]:
        return [f["type"] for f in self.sent]


class Pump:
    """An in-process outbox dispatcher using the REAL Redis publisher."""

    def __init__(self, sessionmaker, settings: Settings) -> None:
        self.publisher = RedisPublisher(settings)
        self.dispatcher = OutboxDispatcher(sessionmaker, self.publisher, settings)

    async def flush(self) -> int:
        """Publish everything currently due. Returns how many rows were processed."""
        total = 0
        while True:
            claimed = await self.dispatcher.run_once()
            if not claimed:
                return total
            total += claimed

    async def aclose(self) -> None:
        await self.publisher.aclose()
