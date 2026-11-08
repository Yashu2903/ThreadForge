"""One Redis Pub/Sub connection per API process, and the room subscriptions on it.

WHAT ARRIVES HERE ARE HINTS. A room channel message says "room R has an event with room_seq N". The
manager validates it and calls `on_room_hint(room, seq)`, which only records "something newer may
exist" and wakes the room's connections; each connection then READS PostgreSQL. Nothing from Redis
is ever forwarded to a browser. A lost, duplicated or reordered hint is therefore harmless: it can
only delay a wake-up, and reconciliation (hub.py) bounds that delay.

DESIGN
  * ONE pub/sub connection. Explicit SUBSCRIBE/UNSUBSCRIBE per room, never PSUBSCRIBE of all rooms
    (a process only receives rooms it serves) and never a consumer group (every API process must
    hear every room it serves). Per room TWO channels: the event hint channel and the presence hint
    channel.
  * ONE task (the "manager") owns every subscribe/unsubscribe, fed by a command queue. A connection
    arriving while its room is mid-subscribe waits behind the command that is subscribing.
  * A room has a reference count of local connections. The last release unsubscribes.
  * A reference is taken even when the subscription cannot be established (Redis down or slow):
    `acquire` then returns False ("degraded") but the reference stays, and the manager's repair pass
    and its reconnect logic subscribe the room as soon as Redis allows. Callers ALWAYS release.
  * A subscription counts as established only when Redis's SUBSCRIBE ACKNOWLEDGEMENT has been read.
  * The READER task does no database or WebSocket work: it parses, validates and calls a cheap
    synchronous callback (an assignment and an Event.set). Memory per room is constant, so a slow
    consumer elsewhere cannot make hints pile up and Redis never has a reason to disconnect us
    (Redis closes pub/sub subscribers whose output buffer grows: client-output-buffer-limit).
  * If the connection drops: reconnect with capped, jittered backoff, re-subscribe every referenced
    room, and call `on_recovered()` so everything wakes and re-reads PostgreSQL.
  * Redis trouble never fails HTTP: nothing here is on the HTTP request path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.config import Settings
from app.logging_config import APP_LOGGER
from app.realtime.channels import (
    presence_hint_channel,
    presence_room_from_channel,
    room_channel,
)
from app.realtime.notifications import NotificationRejected, parse_notification
from app.realtime.publisher import create_redis_client

log = logging.getLogger(f"{APP_LOGGER}.realtime")

RECONNECT_BASE_SECONDS = 0.25
RECONNECT_MAX_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 1.0
REPAIR_INTERVAL_SECONDS = 2.0  # how often missing subscriptions are retried while connected
MAX_PRESENCE_HINT_BYTES = 64
FAST_FAIL_AFTER_SECONDS = 1.0  # Redis down longer than this: acquire fails fast (degraded)


class SubscribeFailed(Exception):
    """The room subscription could not be established in time (Redis down, slow, or stopping)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Command:
    kind: str  # "acquire" | "release" | "resubscribe"
    room_id: uuid.UUID | None = None
    future: asyncio.Future[None] | None = None


def _noop(*_args: Any) -> None:
    return None


class SubscriptionManager:
    def __init__(
        self,
        settings: Settings,
        *,
        on_room_hint: Callable[[uuid.UUID, int], None] = _noop,
        on_presence_hint: Callable[[uuid.UUID], None] = _noop,
        on_recovered: Callable[[], None] = _noop,
        client_factory: Callable[[], Redis] | None = None,
    ) -> None:
        self._settings = settings
        self._prefix = settings.redis_channel_prefix
        self._on_room_hint = on_room_hint
        self._on_presence_hint = on_presence_hint
        self._on_recovered = on_recovered
        self._client_factory = client_factory or (lambda: create_redis_client(settings))
        self._commands: asyncio.Queue[_Command] = asyncio.Queue()
        self._refs: dict[uuid.UUID, int] = {}  # mutated ONLY by the manager task
        self._subscribed: set[str] = set()  # channels whose ack was read on the CURRENT connection
        self._acks: dict[tuple[str, str], asyncio.Future[None]] = {}
        self._pending: dict[uuid.UUID, list[asyncio.Future[None]]] = {}  # acquires awaiting Redis
        self._down_since: float | None = None  # when Redis last became unreachable (None: up)
        self._client: Redis | None = None
        self._pubsub: PubSub | None = None
        self._connected = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        # counters (visible in logs and status())
        self.rejected: Counter[str] = Counter()
        self.connection_losses = 0  # established connections that dropped: a gap is possible
        self.connect_failures = 0
        self.hints_received = 0
        self.presence_hints_received = 0
        self.recoveries = 0

    # ---- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Start the background tasks. Never blocks and never needs Redis to be up."""
        self._down_since = asyncio.get_running_loop().time()
        self._tasks = [
            asyncio.create_task(self._reader_loop(), name="redis-pubsub-reader"),
            asyncio.create_task(self._manager_loop(), name="redis-subscription-manager"),
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        while not self._commands.empty():  # fail callers that were still waiting
            command = self._commands.get_nowait()
            if command.future is not None and not command.future.done():
                command.future.set_exception(SubscribeFailed("shutting_down"))
        self._fail_acks(SubscribeFailed("shutting_down"))
        for futures in self._pending.values():
            for future in futures:
                if not future.done():
                    future.set_exception(SubscribeFailed("shutting_down"))
        self._pending.clear()
        await self._close_connection()

    # ---- public API (called by the WebSocket layer) --------------------------------

    async def acquire(self, room_id: uuid.UUID) -> bool:
        """Add one local connection to the room. True once Redis has ACKed the subscription;
        False if that did not happen in time (degraded mode). Either way the REFERENCE IS HELD and
        the caller must call release_nowait() exactly once; the manager keeps trying to subscribe.
        """
        if self._stopping:
            return False
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        # a result nobody awaits any more (timeout) must not log "exception was never retrieved"
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._commands.put_nowait(_Command("acquire", room_id, future))
        try:
            await asyncio.wait_for(
                asyncio.shield(future), timeout=self._settings.ws_subscribe_timeout_seconds
            )
        except (TimeoutError, SubscribeFailed):
            future.cancel()  # nobody waits any more; the reference itself stays (degraded)
            return False
        except asyncio.CancelledError:
            self.release_nowait(room_id)  # the caller is gone: the acquire (queued) is undone
            raise
        return True

    def release_nowait(self, room_id: uuid.UUID) -> None:
        """Drop one local connection. Fire-and-forget so cleanup paths can never block or be
        cancelled half-way; the manager task performs the unsubscribe in order."""
        self._commands.put_nowait(_Command("release", room_id))

    async def wait_idle(self) -> None:
        """Wait until every queued subscribe/unsubscribe has been processed (tests, shutdown)."""
        await self._commands.join()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def healthy(self) -> bool:
        """Connected AND every referenced room subscribed. Anything else is degraded."""
        return self._connected.is_set() and all(
            self._channels(room)[0] in self._subscribed for room in self._refs
        )

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._connected.is_set(),
            "healthy": self.healthy,
            "subscribed_rooms": len({c for c in self._subscribed if ":room:" in c}),
            "rooms_with_connections": len(self._refs),
            "connection_losses": self.connection_losses,
            "connect_failures": self.connect_failures,
            "hints_received": self.hints_received,
            "rejected_notifications": dict(self.rejected),
            "recoveries": self.recoveries,
        }

    @property
    def subscribed_channels(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    @property
    def reference_counts(self) -> dict[uuid.UUID, int]:
        return dict(self._refs)

    # ---- the reader: parse, validate, call a cheap callback. Nothing else. ---------

    async def _reader_loop(self) -> None:
        delay = RECONNECT_BASE_SECONDS
        while not self._stopping:
            try:
                await self._connect()
                delay = RECONNECT_BASE_SECONDS
                await self._read_forever()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._on_connection_error(exc)
            await self._close_connection()
            if self._stopping:
                return
            await asyncio.sleep(delay / 2 + random.random() * delay / 2)  # noqa: S311 - jitter
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    async def _connect(self) -> None:
        client = self._client_factory()
        pubsub = client.pubsub()
        self._client, self._pubsub = client, pubsub
        await pubsub.connect()  # raises while Redis is unreachable
        self._subscribed.clear()
        self._down_since = None
        self._connected.set()
        log.info("redis_pubsub_connected rooms_to_resubscribe=%s", len(self._refs))
        self._commands.put_nowait(_Command("resubscribe"))

    async def _read_forever(self) -> None:
        pubsub = self._pubsub
        assert pubsub is not None
        while True:
            if not pubsub.subscribed:  # idle: no rooms. Reading would raise; wait for a subscribe
                self._wake.clear()
                if not pubsub.subscribed:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=READ_TIMEOUT_SECONDS)
                continue
            message = await pubsub.get_message(
                ignore_subscribe_messages=False, timeout=READ_TIMEOUT_SECONDS
            )
            if message is not None:
                self._handle(message)

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        channel = message.get("channel")
        if kind in ("subscribe", "unsubscribe"):  # the acknowledgements
            future = self._acks.get((kind, channel))
            if future is not None and not future.done():
                future.set_result(None)
            return
        if kind != "message" or not isinstance(channel, str):
            return
        data = message.get("data", "")
        presence_room = presence_room_from_channel(self._prefix, channel)
        if presence_room is not None:  # the payload is ignored: it only says "look again"
            if len(data) <= MAX_PRESENCE_HINT_BYTES:
                self.presence_hints_received += 1
                self._on_presence_hint(presence_room)
            else:
                self.rejected["presence_oversized"] += 1
            return
        self.hints_received += 1
        try:
            notification = parse_notification(self._prefix, channel, data)
        except NotificationRejected as exc:
            self.rejected[exc.reason] += 1
            log.warning(
                "notification_rejected reason=%s total=%s", exc.reason, self.rejected[exc.reason]
            )
            return
        # Only the room and the sequence number are used: a HINT, never an event.
        self._on_room_hint(notification.room_id, notification.room_seq)

    def _on_connection_error(self, exc: Exception) -> None:
        was_established = self._connected.is_set()
        if self._down_since is None:
            self._down_since = asyncio.get_running_loop().time()
        self._connected.clear()
        self._subscribed.clear()
        self._fail_acks(ConnectionError("pubsub connection lost"))
        if was_established:
            self.connection_losses += 1
            log.warning(
                "redis_pubsub_lost error_type=%s notification_gap_possible=true losses=%s "
                "(hints published while disconnected are lost; reconciliation covers them)",
                type(exc).__name__,
                self.connection_losses,
            )
        else:
            self.connect_failures += 1
            log.warning(
                "redis_pubsub_connect_failed error_type=%s failures=%s",
                type(exc).__name__,
                self.connect_failures,
            )

    async def _close_connection(self) -> None:
        pubsub, client = self._pubsub, self._client
        self._pubsub = self._client = None
        self._connected.clear()
        self._subscribed.clear()  # a closed connection holds no subscriptions
        for closer in (pubsub, client):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(closer.aclose(), timeout=2)

    def _fail_acks(self, exc: BaseException) -> None:
        for future in list(self._acks.values()):
            if not future.done():
                future.set_exception(exc)
        self._acks.clear()

    # ---- the manager: the ONLY place subscriptions change --------------------------

    async def _manager_loop(self) -> None:
        while True:
            try:
                command = await asyncio.wait_for(
                    self._commands.get(), timeout=REPAIR_INTERVAL_SECONDS
                )
            except TimeoutError:
                await self._repair()
                continue
            try:
                if command.kind == "acquire":
                    await self._do_acquire(command)
                elif command.kind == "release":
                    await self._do_release(command)
                elif command.kind == "resubscribe":
                    await self._do_resubscribe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one bad command kill the manager
                log.error(
                    "subscription_manager_error kind=%s error_type=%s",
                    command.kind,
                    type(exc).__name__,
                )
            finally:
                self._commands.task_done()

    def _channels(self, room_id: uuid.UUID) -> list[str]:
        return [room_channel(self._prefix, room_id), presence_hint_channel(self._prefix, room_id)]

    def _missing(self) -> list[str]:
        return [c for room in self._refs for c in self._channels(room) if c not in self._subscribed]

    async def _do_acquire(self, command: _Command) -> None:
        future, room_id = command.future, command.room_id
        assert future is not None and room_id is not None
        self._refs[room_id] = self._refs.get(room_id, 0) + 1  # the reference is ALWAYS taken
        channels = self._channels(room_id)
        if all(c in self._subscribed for c in channels):
            if not future.done():
                future.set_result(None)
            return
        if not self._connected.is_set():
            down_for = asyncio.get_running_loop().time() - (self._down_since or 0.0)
            if down_for > FAST_FAIL_AFTER_SECONDS:  # known to be down: do not make callers wait
                if not future.done():
                    future.set_exception(SubscribeFailed("redis_unavailable"))
                return
            waiting = self._pending.setdefault(room_id, [])
            waiting[:] = [f for f in waiting if not f.done()]
            waiting.append(future)  # resolved when the connect + subscribe completes
            return
        try:
            await self._subscribe(channels)
        except Exception as exc:
            if not future.done():
                reason = exc.reason if isinstance(exc, SubscribeFailed) else type(exc).__name__
                future.set_exception(SubscribeFailed(reason))
            return
        if not future.done():
            future.set_result(None)

    async def _do_release(self, command: _Command) -> None:
        room_id = command.room_id
        assert room_id is not None
        remaining = self._refs.get(room_id, 0) - 1
        if remaining > 0:
            self._refs[room_id] = remaining
            return
        self._refs.pop(room_id, None)
        for future in self._pending.pop(room_id, []):
            future.cancel()
        channels = [c for c in self._channels(room_id) if c in self._subscribed]
        if channels and self._connected.is_set():
            with contextlib.suppress(Exception):  # a dead connection is already unsubscribed
                await self._unsubscribe(channels)
        self._subscribed.difference_update(self._channels(room_id))

    async def _do_resubscribe(self) -> None:
        channels = self._missing()
        if channels and self._connected.is_set():
            try:
                await self._subscribe(channels)
            except Exception as exc:  # the reader will reconnect and enqueue another resubscribe
                log.error("resubscribe_failed error_type=%s", type(exc).__name__)
                return
            log.warning(
                "redis_pubsub_resubscribed channels=%s notification_gap_possible=true",
                len(channels),
            )
        if self._connected.is_set():
            self._recovered()

    async def _repair(self) -> None:
        """Retry subscriptions that never completed (an ack timeout while connected)."""
        channels = self._missing()
        if not channels or not self._connected.is_set():
            return
        try:
            await self._subscribe(channels)
        except Exception as exc:
            log.warning("subscription_repair_failed error_type=%s", type(exc).__name__)
            return
        self._recovered()

    def _resolve_pending(self) -> None:
        for room_id in list(self._pending):
            if self._channels(room_id)[0] in self._subscribed:
                for future in self._pending.pop(room_id):
                    if not future.done():
                        future.set_result(None)

    def _recovered(self) -> None:
        self._resolve_pending()
        self.recoveries += 1
        try:
            self._on_recovered()
        except Exception as exc:
            log.error("on_recovered_failed error_type=%s", type(exc).__name__)

    async def _connected_pubsub(self) -> PubSub:
        try:
            await asyncio.wait_for(
                self._connected.wait(), timeout=self._settings.ws_subscribe_timeout_seconds
            )
        except TimeoutError:
            raise SubscribeFailed("redis_unavailable") from None
        assert self._pubsub is not None
        return self._pubsub

    async def _subscribe(self, channels: list[str]) -> None:
        pubsub = await self._connected_pubsub()
        loop = asyncio.get_running_loop()
        futures = {c: loop.create_future() for c in channels}
        for channel, future in futures.items():
            self._acks[("subscribe", channel)] = future
        try:
            await pubsub.subscribe(*channels)
            self._wake.set()
            # Established only when Redis's acknowledgement has been READ (not when sent).
            await asyncio.wait_for(
                asyncio.gather(*futures.values()),
                timeout=self._settings.ws_subscribe_timeout_seconds,
            )
        except TimeoutError:
            raise SubscribeFailed("ack_timeout") from None
        finally:
            for channel in channels:
                self._acks.pop(("subscribe", channel), None)
        self._subscribed.update(channels)

    async def _unsubscribe(self, channels: list[str]) -> None:
        pubsub = await self._connected_pubsub()
        loop = asyncio.get_running_loop()
        futures = {c: loop.create_future() for c in channels}
        for channel, future in futures.items():
            self._acks[("unsubscribe", channel)] = future
        try:
            await pubsub.unsubscribe(*channels)
            await asyncio.wait_for(
                asyncio.gather(*futures.values()),
                timeout=self._settings.ws_subscribe_timeout_seconds,
            )
        finally:
            for channel in channels:
                self._acks.pop(("unsubscribe", channel), None)
