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
