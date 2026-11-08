"""WebSocket fan-out inside one API process.

    Redis --> SubscriptionManager reader --> bounded notification queue --> forwarding workers
          --> each connection's bounded send queue --> that connection's sender task --> browser

What is durable and what is not: PostgreSQL holds the events. Everything here is memory: a
notification is a HINT that an event exists. It can be lost (queue overflow, Redis gap, a client
that was not connected) and can arrive twice or out of order; a frame sent proves nothing about
browser receipt. Clients de-duplicate by event_id and order by room_seq; recovery is Part 5.

CONNECTION LIFECYCLE (serve)
  1. (route) authenticate + authorize before accept; every failure is the same HTTP 403.
  2. accept; enforce the connection caps (over the cap: close 4409).
  3. register the connection, NOT yet live, with a bounded send queue and a sender task.
  4. ask the subscription manager for the room and wait for Redis's acknowledgement (bounded; on
     failure close 1013).
  5. read the room's current event_seq (a short transaction) = the high-water MARK.
  6. enqueue the ready frame FIRST, then the events that arrived while we were not yet live and are
     ABOVE the mark, then flip live: no await between these steps.
     (Events at or below the mark were committed before the mark was read: history has them, so they
     are dropped here. Events above it are held and delivered, otherwise one committed just after
     the mark was read, but handled before the flip, would be skipped by this connection.)

FORWARDING one notification: re-read the event from PostgreSQL (verify event_id, room_id and seq
agree), run ONE query for all of the room's local connections (session still valid? member still
active?), then per connection: close it (4401 / 4403) or enqueue the frame with put_nowait, closing
it with 4429 if its queue is full. A removed member never receives their own member_removed frame:
the close code is their signal. Forwarding performs NO database writes.

LIMITATION (Part 5): an idle connection whose session expired, or whose member was removed, is only
noticed when the NEXT event for its room is forwarded. Part 4 does not claim live revocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import Counter, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import APP_LOGGER
from app.realtime import protocol
from app.realtime.notifications import Notification
from app.realtime.subscriptions import SubscribeFailed, SubscriptionManager
from app.services import realtime_access
from app.services.realtime_access import HandshakeIdentity

log = logging.getLogger(f"{APP_LOGGER}.realtime")

STATS_INTERVAL_SECONDS = 30.0
RECENT_EVENT_IDS = 64  # per-connection duplicate-frame suppression window (clients still dedupe)


class WebSocketLike(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...

    async def receive(self) -> dict[str, Any]: ...


@dataclass
class HubCounters:
    connections_accepted: int = 0
    connections_rejected_cap: int = 0
    frames_sent: int = 0
    events_forwarded: int = 0
    events_dropped_unverifiable: int = 0
    slow_consumer_disconnects: int = 0
    send_timeouts: int = 0
    closed_session_invalid: int = 0
    closed_access_revoked: int = 0
    subscribe_failures: int = 0


class Connection:
    """One browser socket. Holds ids (user, session), never the raw cookie secret."""

    def __init__(
        self,
        hub: RealtimeHub,
        websocket: WebSocketLike,
        room_id: uuid.UUID,
        identity: HandshakeIdentity,
    ) -> None:
        self.hub = hub
        self.websocket = websocket
        self.room_id = room_id
        self.user_id = identity.user_id
        self.session_id = identity.session_id
        self.id = uuid.uuid4()
        self.queue: asyncio.Queue[str] = asyncio.Queue(
            maxsize=hub.settings.ws_connection_queue_size
        )
        self.pending: list[
            tuple[int, str]
        ] = []  # (room_seq, frame) held until the connection is live
        self.live = False
        self.closing = False
        self.close_code: int | None = None
        self.closed = asyncio.Event()
        self.sender_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._recent: deque[uuid.UUID] = deque(maxlen=RECENT_EVENT_IDS)
        self._recent_set: set[uuid.UUID] = set()

    def is_duplicate(self, event_id: uuid.UUID) -> bool:
        if event_id in self._recent_set:
            return True
        if len(self._recent) == self._recent.maxlen:
            self._recent_set.discard(self._recent[0])
        self._recent.append(event_id)
        self._recent_set.add(event_id)
        return False

    def start_sender(self) -> None:
        self.sender_task = asyncio.create_task(self._sender(), name=f"ws-sender-{self.id}")

    async def _sender(self) -> None:
        timeout = self.hub.settings.ws_send_timeout_seconds
        try:
            while True:
                frame = await self.queue.get()
                # Every send is bounded: a stalled TCP connection cannot block this task forever.
                await asyncio.wait_for(self.websocket.send_text(frame), timeout=timeout)
                self.hub.counters.frames_sent += 1
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.hub.counters.send_timeouts += 1
            self.hub.counters.slow_consumer_disconnects += 1
            log.warning("ws_send_timeout room_id=%s connection=%s: closing", self.room_id, self.id)
            self.close_nowait(protocol.CLOSE_SLOW_CONSUMER)
        except Exception:  # the client went away mid-send
            self.close_nowait(1000)

    def close_nowait(self, code: int) -> None:
        """Stop routing frames here at once (synchronously) and close the socket in the background,
        so one stalled client can never block a forwarding worker."""
        if self.closing:
            return
        self.closing = True
        self.live = False
        self.close_code = code
        self.hub._unregister(self)
        self.closed.set()
        self._close_task = self.hub._spawn(self._finish_close(code))

    async def _finish_close(self, code: int) -> None:
        current = asyncio.current_task()
        if self.sender_task is not None and self.sender_task is not current:
            self.sender_task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.websocket.close(code=code), timeout=self.hub.settings.ws_send_timeout_seconds
            )

    async def wait_close_finished(self) -> None:
        task = self._close_task
        if task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self.hub.settings.ws_send_timeout_seconds + 1
                )


class RealtimeHub:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        redis_client_factory: Callable[[], Redis] | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.notifications: asyncio.Queue[Notification] = asyncio.Queue(
            maxsize=settings.realtime_queue_size
        )
        self.manager = SubscriptionManager(
            settings, self.notifications, client_factory=redis_client_factory
        )
        self.rooms: dict[uuid.UUID, dict[uuid.UUID, Connection]] = {}
        self.per_user: Counter[uuid.UUID] = Counter()
        self.counters = HubCounters()
        self._workers: list[asyncio.Task[None]] = []
        self._background: set[asyncio.Task[None]] = set()
        self._stats_task: asyncio.Task[None] | None = None
        self.stopping = False

    # ---- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Never needs Redis: the manager connects (and reconnects) in the background."""
        await self.manager.start()
        self._workers = [
            asyncio.create_task(self._forward_worker(), name=f"ws-forward-{i}")
            for i in range(self.settings.ws_forward_workers)
        ]
        self._stats_task = asyncio.create_task(self._stats_loop(), name="ws-stats")

    async def stop(self) -> None:
        """Shutdown: close every socket with 1001, then stop workers and the Redis connection."""
        self.stopping = True
        for conn in list(self.all_connections()):
            conn.close_nowait(protocol.CLOSE_GOING_AWAY)
        if self._background:
            await asyncio.wait(
                set(self._background), timeout=self.settings.ws_send_timeout_seconds + 2
            )
        tasks = [*self._workers, *([self._stats_task] if self._stats_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers, self._stats_task = [], None
        await self.manager.stop()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ---- registry ------------------------------------------------------------------

    def all_connections(self) -> list[Connection]:
        return [c for room in self.rooms.values() for c in room.values()]

    @property
    def active_connections(self) -> int:
        return sum(len(room) for room in self.rooms.values())

    def admit(self, user_id: uuid.UUID) -> bool:
        return (
            self.per_user[user_id] < self.settings.ws_max_connections_per_user
            and self.active_connections < self.settings.ws_max_connections_per_process
        )

    def _register(self, conn: Connection) -> None:
        self.rooms.setdefault(conn.room_id, {})[conn.id] = conn
        self.per_user[conn.user_id] += 1

    def _unregister(self, conn: Connection) -> None:
        room = self.rooms.get(conn.room_id)
        if room is None or room.pop(conn.id, None) is None:
            return
        if not room:
            del self.rooms[conn.room_id]
        self.per_user[conn.user_id] -= 1
        if self.per_user[conn.user_id] <= 0:
            del self.per_user[conn.user_id]

    # ---- the handshake helper used by the route ------------------------------------

    async def authenticate(
        self, secret: str | None, room_id: uuid.UUID | None
    ) -> HandshakeIdentity | None:
        return await realtime_access.authenticate_websocket(self.sessionmaker, secret, room_id)

    # ---- one connection ------------------------------------------------------------

    async def serve(
        self, websocket: WebSocketLike, room_id: uuid.UUID, identity: HandshakeIdentity
    ) -> None:
        """Run an ALREADY-AUTHORIZED connection: accept (the route does this), register, subscribe,
        say ready, then watch for disconnects. See the module docstring for the exact order."""
        conn = Connection(self, websocket, room_id, identity)
        if self.stopping or not self.admit(identity.user_id):
            self.counters.connections_rejected_cap += 1
            await _quiet_close(
                websocket,
                protocol.CLOSE_TOO_MANY_CONNECTIONS,
                self.settings.ws_send_timeout_seconds,
            )
            return
        self._register(conn)  # no await between admit() and register(): the caps are exact
        self.counters.connections_accepted += 1
        conn.start_sender()
        acquired = False
        try:
            try:
                await self.manager.acquire(room_id)  # waits for Redis's SUBSCRIBE acknowledgement
                acquired = True
            except SubscribeFailed as exc:
                self.counters.subscribe_failures += 1
                log.warning("ws_subscribe_failed room_id=%s reason=%s", room_id, exc.reason)
                conn.close_nowait(protocol.CLOSE_TRY_AGAIN_LATER)
                return
            mark = await realtime_access.read_event_seq(self.sessionmaker, room_id)
            # --- no await from here to the end of this block ---
            try:
                conn.queue.put_nowait(protocol.ready_frame(room_id, mark))
                for seq, frame in conn.pending:
                    if seq > mark:
                        conn.queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._slow(conn)
                return
            conn.pending.clear()
            conn.live = not conn.closing
            # ---------------------------------------------------
            await self._receive_loop(conn)
        finally:
            self._unregister(conn)
            if acquired:
                self.manager.release_nowait(room_id)
            if conn.sender_task is not None:
                conn.sender_task.cancel()
                await asyncio.gather(conn.sender_task, return_exceptions=True)
            await conn.wait_close_finished()
            if not conn.closing:  # the client is gone or the handler is exiting: tidy up
                await _quiet_close(websocket, 1000, self.settings.ws_send_timeout_seconds)

    async def _receive_loop(self, conn: Connection) -> None:
        """Exists only to notice disconnects and to reject client frames (no commands exist)."""
        closed_wait = asyncio.ensure_future(conn.closed.wait())
        try:
            while not conn.closing:
                receive = asyncio.ensure_future(conn.websocket.receive())
                done, _ = await asyncio.wait(
                    {receive, closed_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if receive not in done:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                    return
                try:
                    message = receive.result()
                except Exception:  # e.g. receive() after the socket is already gone
                    return
                if message.get("type") == "websocket.disconnect":
                    return
                text, data = message.get("text"), message.get("bytes")
                if text is not None:
                    too_big = len(text.encode("utf-8")) > protocol.MAX_CLIENT_FRAME_BYTES
                    conn.close_nowait(
                        protocol.CLOSE_MESSAGE_TOO_BIG
                        if too_big
                        else protocol.CLOSE_POLICY_VIOLATION
                    )
                elif data is not None:
                    too_big = len(data) > protocol.MAX_CLIENT_FRAME_BYTES
                    conn.close_nowait(
                        protocol.CLOSE_MESSAGE_TOO_BIG
                        if too_big
                        else protocol.CLOSE_UNSUPPORTED_DATA
                    )
        finally:
            closed_wait.cancel()
            await asyncio.gather(closed_wait, return_exceptions=True)

    def _slow(self, conn: Connection) -> None:
        self.counters.slow_consumer_disconnects += 1
        log.warning(
            "ws_slow_consumer room_id=%s connection=%s: closing with 4429", conn.room_id, conn.id
        )
        conn.close_nowait(protocol.CLOSE_SLOW_CONSUMER)

    # ---- forwarding ----------------------------------------------------------------

    async def _forward_worker(self) -> None:
        while True:
            notification = await self.notifications.get()
            try:
                await self._forward(notification)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failure here must never kill the worker
                log.error(
                    "ws_forward_failed event_id=%s room_id=%s error_type=%s",
                    notification.event_id,
                    notification.room_id,
                    type(exc).__name__,
                )

    async def _forward(self, notification: Notification) -> None:
        room_id = notification.room_id
        if not self.rooms.get(room_id):
            return  # no local connection for this room: not even a database read
        event = await realtime_access.fetch_event(
            self.sessionmaker, room_id, notification.event_id, notification.room_seq
        )
        if event is None:
            self.counters.events_dropped_unverifiable += 1
            log.warning(
                "ws_event_unverifiable event_id=%s room_id=%s room_seq=%s: dropped",
                notification.event_id,
                room_id,
                notification.room_seq,
            )
            return
        # ONE query covers every local connection of this room (not one per connection).
        connections = [c for c in self.rooms.get(room_id, {}).values() if not c.closing]
        access = await realtime_access.check_access(
            self.sessionmaker, room_id, [c.session_id for c in connections]
        )
        frame = protocol.event_frame(event)
        for conn in connections:
            if conn.closing:
                continue
            verdict = access.get(conn.session_id)
            if verdict is None or not verdict.session_ok:
                self.counters.closed_session_invalid += 1
                conn.close_nowait(protocol.CLOSE_SESSION_INVALID)
            elif not verdict.member_ok:
                self.counters.closed_access_revoked += 1
                conn.close_nowait(
                    protocol.CLOSE_ACCESS_REVOKED
                )  # no frame: not even member_removed
            elif conn.is_duplicate(event.event_id):
                continue  # already sent to this connection (e.g. the dispatcher published twice)
            elif not conn.live:
                if len(conn.pending) >= self.settings.ws_connection_queue_size:
                    conn.close_nowait(protocol.CLOSE_TRY_AGAIN_LATER)
                else:
                    conn.pending.append((event.room_seq, frame))  # delivered once live if > mark
            else:
                try:
                    conn.queue.put_nowait(frame)
                    self.counters.events_forwarded += 1
                except asyncio.QueueFull:
                    self._slow(conn)  # disconnect this one; the others keep receiving

    # ---- observability -------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "active_connections": self.active_connections,
            "rooms_with_connections": len(self.rooms),
            "redis": self.manager.status(),
            **self.counters.__dict__,
        }

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_SECONDS)
            s = self.stats()
            redis = s["redis"]
            log.info(
                "realtime_stats connections=%s rooms=%s slow_consumer_disconnects=%s "
                "redis_connected=%s subscribed_rooms=%s dropped_notifications=%s "
                "connection_losses=%s frames_sent=%s",
                s["active_connections"],
                s["rooms_with_connections"],
                s["slow_consumer_disconnects"],
                redis["connected"],
                redis["subscribed_rooms"],
                redis["dropped_notifications"],
                redis["connection_losses"],
                s["frames_sent"],
            )


async def _quiet_close(websocket: WebSocketLike, code: int, within: float) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(websocket.close(code=code), timeout=within)
