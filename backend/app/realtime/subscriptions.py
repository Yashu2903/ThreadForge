"""One Redis Pub/Sub connection per API process, and the room subscriptions on it.

DESIGN
  * ONE pub/sub connection. Explicit SUBSCRIBE/UNSUBSCRIBE per room channel, never PSUBSCRIBE of all
    rooms (a process only receives rooms it serves) and never a consumer group (every API process
    must receive every event for the rooms it serves).
  * ONE task (the "manager") owns every subscribe/unsubscribe, fed by a command queue. A connection
    arriving while its room is mid-subscribe simply waits behind the command that is subscribing; a
    connection arriving mid-unsubscribe waits and then re-subscribes. Races become ordering.
  * A room has a reference count of local connections. The last release unsubscribes.
  * A subscription counts as established only when Redis's SUBSCRIBE ACKNOWLEDGEMENT has been read,
    not when the command was sent.
  * The READER task does no database or WebSocket work. It parses, validates and puts a small
    Notification on a bounded queue. Redis disconnects a Pub/Sub subscriber whose output buffer
    grows too large (client-output-buffer-limit pubsub: 32 MB hard, 8 MB for 60 s by default;
    documented at redis.io "Redis client handling"), so a slow reader would be cut off. When the
    queue is full the OLDEST notification is dropped and counted: losing a hint is acceptable
    (PostgreSQL holds the history; Part 5 handles recovery), losing the connection is not.
  * If the connection drops: reconnect with capped, jittered exponential backoff, re-subscribe every
    room with a positive reference count, and log notification_gap_possible. A restored subscription
    does NOT recover notifications published while it was down.
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
from app.realtime.channels import room_channel
from app.realtime.notifications import Notification, NotificationRejected, parse_notification
from app.realtime.publisher import create_redis_client

log = logging.getLogger(f"{APP_LOGGER}.realtime")

RECONNECT_BASE_SECONDS = 0.25
RECONNECT_MAX_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 1.0


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


class SubscriptionManager:
    def __init__(
        self,
        settings: Settings,
        notifications: asyncio.Queue[Notification],
        *,
        client_factory: Callable[[], Redis] | None = None,
    ) -> None:
        self._settings = settings
        self._prefix = settings.redis_channel_prefix
        self._notifications = notifications
        self._client_factory = client_factory or (lambda: create_redis_client(settings))
        self._commands: asyncio.Queue[_Command] = asyncio.Queue()
        self._refs: dict[uuid.UUID, int] = {}  # mutated ONLY by the manager task
        self._subscribed: set[str] = set()  # channels whose ack was read on the CURRENT connection
        self._acks: dict[tuple[str, str], asyncio.Future[None]] = {}
        self._client: Redis | None = None
        self._pubsub: PubSub | None = None
        self._connected = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        # counters (visible in logs and status())
        self.dropped_notifications = 0
        self.rejected: Counter[str] = Counter()
        self.connection_losses = 0  # established connections that dropped: a gap is possible
        self.connect_failures = 0
        self.notifications_received = 0

    # ---- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Start the background tasks. Never blocks and never needs Redis to be up."""
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
        await self._close_connection()

    # ---- public API (called by the WebSocket layer) --------------------------------

    async def acquire(self, room_id: uuid.UUID) -> None:
        """Add one local connection to the room and wait until Redis has ACKed the subscription.

        Raises SubscribeFailed on timeout or Redis trouble. If it raises, no reference is held.
        """
        if self._stopping:
            raise SubscribeFailed("shutting_down")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._commands.put_nowait(_Command("acquire", room_id, future))
        try:
            await asyncio.wait_for(future, timeout=self._settings.ws_subscribe_timeout_seconds)
        except TimeoutError:
            # wait_for cancelled the future; the manager notices and gives back any reference.
            raise SubscribeFailed("timeout") from None

    def release_nowait(self, room_id: uuid.UUID) -> None:
        """Drop one local connection. Fire-and-forget so cleanup paths can never block or be
        cancelled half-way; the manager task performs the unsubscribe in order."""
        self._commands.put_nowait(_Command("release", room_id))

    async def wait_idle(self) -> None:
        """Wait until every queued subscribe/unsubscribe has been processed (tests, shutdown)."""
        await self._commands.join()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._connected.is_set(),
            "subscribed_rooms": len(self._subscribed),
            "rooms_with_connections": len(self._refs),
            "connection_losses": self.connection_losses,
            "connect_failures": self.connect_failures,
            "dropped_notifications": self.dropped_notifications,
            "rejected_notifications": dict(self.rejected),
            "notification_queue_depth": self._notifications.qsize(),
        }

    @property
    def subscribed_channels(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    @property
    def reference_counts(self) -> dict[uuid.UUID, int]:
        return dict(self._refs)

    # ---- the reader: parse, validate, enqueue. Nothing else. -----------------------

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
        if kind != "message":
            return
        self.notifications_received += 1
        try:
            notification = parse_notification(self._prefix, channel, message.get("data", ""))
        except NotificationRejected as exc:
            self.rejected[exc.reason] += 1
            log.warning(
                "notification_rejected reason=%s total=%s", exc.reason, self.rejected[exc.reason]
            )
            return
        self._enqueue(notification)

    def _enqueue(self, notification: Notification) -> None:
        try:
            self._notifications.put_nowait(notification)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._notifications.get_nowait()  # drop the OLDEST
            self.dropped_notifications += 1
            log.warning("notification_dropped_queue_full total=%s", self.dropped_notifications)
            self._notifications.put_nowait(notification)

    def _on_connection_error(self, exc: Exception) -> None:
        was_established = self._connected.is_set()
        self._connected.clear()
        self._subscribed.clear()
        self._fail_acks(ConnectionError("pubsub connection lost"))
        if was_established:
            self.connection_losses += 1
            log.warning(
                "redis_pubsub_lost error_type=%s notification_gap_possible=true losses=%s "
                "(notifications published while disconnected are NOT recovered)",
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
            command = await self._commands.get()
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

    def _channel(self, room_id: uuid.UUID) -> str:
        return room_channel(self._prefix, room_id)

    async def _do_acquire(self, command: _Command) -> None:
        future, room_id = command.future, command.room_id
        assert future is not None and room_id is not None
        if future.done():  # the caller already timed out or was cancelled: take no reference
            return
        self._refs[room_id] = self._refs.get(room_id, 0) + 1
        channel = self._channel(room_id)
        try:
            if channel not in self._subscribed:
                await self._subscribe([channel])
        except Exception as exc:
            self._decrement(room_id)
            if not future.done():
                reason = exc.reason if isinstance(exc, SubscribeFailed) else type(exc).__name__
                future.set_exception(SubscribeFailed(reason))
            return
        if future.done():  # cancelled while we were subscribing: hand the reference back
            await self._do_release(_Command("release", room_id))
        else:
            future.set_result(None)

    async def _do_release(self, command: _Command) -> None:
        room_id = command.room_id
        assert room_id is not None
        if not self._decrement(room_id):
            return  # still has local connections
        channel = self._channel(room_id)
        if channel in self._subscribed:
            with contextlib.suppress(Exception):  # a dead connection is already unsubscribed
                await self._unsubscribe(channel)
            self._subscribed.discard(channel)

    def _decrement(self, room_id: uuid.UUID) -> bool:
        """Drop one reference. True when the room now has none."""
        remaining = self._refs.get(room_id, 0) - 1
        if remaining > 0:
            self._refs[room_id] = remaining
            return False
        self._refs.pop(room_id, None)
        return True

    async def _do_resubscribe(self) -> None:
        channels = [self._channel(room_id) for room_id in self._refs]
        if channels and self._connected.is_set():
            try:
                await self._subscribe(channels)
                log.warning(
                    "redis_pubsub_resubscribed rooms=%s notification_gap_possible=true",
                    len(channels),
                )
            except Exception as exc:  # the reader will reconnect and enqueue another resubscribe
                log.error("resubscribe_failed error_type=%s", type(exc).__name__)

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

    async def _unsubscribe(self, channel: str) -> None:
        pubsub = await self._connected_pubsub()
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._acks[("unsubscribe", channel)] = future
        try:
            await pubsub.unsubscribe(channel)
            await asyncio.wait_for(future, timeout=self._settings.ws_subscribe_timeout_seconds)
        finally:
            self._acks.pop(("unsubscribe", channel), None)
