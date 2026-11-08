"""Per-process coordination of presence and typing: workers, ticks, filtering, degraded mode.

PIPELINE. Nothing here runs on a connection's receiver task. A heartbeat, a typing frame or an
availability change becomes a small operation put on a bounded queue (put_nowait: a full queue
drops the operation and counts it, never blocks a socket). A few worker tasks run the operations
against Redis; every Redis call is bounded by a timeout.

TICKS. Presence is a snapshot rebuilt from Redis. Redis never announces an expiry, so each process
re-reads every PRESENCE_TICK seconds and sends a full presence_snapshot to the room's live local
connections IF IT CHANGED. A change made anywhere (connect, disconnect, availability, typing)
publishes a tiny hint on {prefix}:presence-hint:{room}; processes re-tick on it, coalesced to at
most one presence tick per second and two typing ticks per second per room. A lost hint costs at
most one tick.

FILTERING. Every snapshot is filtered to the room's CURRENT active members (a cached set that is
refreshed when a member event is seen and at least every 15 s), so a removed user never appears.

DEGRADED. If Redis cannot answer, snapshots say `degraded: true` with nothing in them: presence is
UNKNOWN, not "everyone is offline". When Redis answers again, this process immediately
re-registers all its local connections (a Redis restart or flush rebuilds presence) and ticks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.logging_config import APP_LOGGER
from app.realtime import protocol
from app.realtime.connection import AlreadyClosing, CloseConnection, Connection
from app.realtime.presence import PresenceStore, PresenceUnavailable
from app.services import realtime_access

if TYPE_CHECKING:
    from app.realtime.room_hub import RealtimeHub, RoomState

log = logging.getLogger(f"{APP_LOGGER}.realtime")

WORKERS = 4
QUEUE_SIZE = 10_000
MEMBER_CACHE_SECONDS = 15.0
REREGISTER_CONCURRENCY = 20


class PresenceCoordinator:
    def __init__(self, hub: RealtimeHub, store: PresenceStore | None = None) -> None:
        self.hub = hub
        self.settings: Settings = hub.settings
        self.store = store or PresenceStore(hub.settings)
        self.ops: asyncio.Queue[tuple[Any, ...]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.ok = True  # False after a Redis failure, until an operation succeeds again
        self._tasks: list[asyncio.Task[None]] = []
        self._timers: dict[tuple[uuid.UUID, str], asyncio.TimerHandle] = {}
        self._last_run: dict[tuple[uuid.UUID, str], float] = {}
        self.dropped_ops = 0
        self.failures = 0
        self.recoveries = 0
        self.ticks = 0

    # ---- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._worker(), name=f"presence-worker-{i}") for i in range(WORKERS)
        ]
        self._tasks.append(asyncio.create_task(self._tick_loop(), name="presence-tick"))

    async def stop(self) -> None:
        with contextlib.suppress(Exception):  # best-effort flush of "connection closed" operations
            await asyncio.wait_for(self.ops.join(), timeout=2)
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.store.aclose()

    # ---- what connections call (all non-blocking) -------------------------------------------

    def submit(self, *op: Any) -> None:
        try:
            self.ops.put_nowait(op)
        except asyncio.QueueFull:
            self.dropped_ops += 1

    def touch(self, conn: Connection) -> None:
        if conn.live and not conn.touch_pending:
            conn.touch_pending = True
            self.submit("touch", conn)

    def typing(self, conn: Connection, state: str) -> None:
        self.submit("typing", conn, state)

    def availability(self, conn: Connection, value: str) -> None:
        self.submit("avail", conn, value)

    def connection_live(self, conn: Connection) -> None:
        self.submit("live", conn)

    def connection_closed(self, conn: Connection) -> None:
        if conn.presence_registered or conn.live:
            self.submit("closed", conn.room_id, conn.user_id, conn.id)

    def on_hint(self, room_id: uuid.UUID) -> None:
        if room_id in self.hub.rooms:
            self.schedule(room_id, "presence")
            self.schedule(room_id, "typing")

    def on_redis_lost(self) -> None:
        for room_id in list(self.hub.rooms):  # find out NOW whether presence is still readable
            self.schedule(room_id, "presence")
            self.schedule(room_id, "typing")

    def on_recovered(self) -> None:
        self.submit("reregister_all")

    def note_member_event(self, room_id: uuid.UUID) -> None:
        state = self.hub.rooms.get(room_id)
        if state is not None:
            state.members_stale = True
            self.schedule(room_id, "presence")
            self.schedule(room_id, "typing")

    # ---- scheduling (coalesced) -------------------------------------------------------------

    def schedule(self, room_id: uuid.UUID, kind: str, *, after: float = 0.0) -> None:
        key = (room_id, kind)
        if key in self._timers:
            return  # one pending tick per room and kind: everything in between is coalesced
        s = self.settings
        minimum = (
            s.presence_hint_min_interval_seconds
            if kind == "presence"
            else s.typing_state_min_interval_seconds
        )
        loop = asyncio.get_running_loop()
        delay = max(after, self._last_run.get(key, float("-inf")) + minimum - loop.time(), 0.0)
        self._timers[key] = loop.call_later(delay, self._fire, room_id, kind)

    def _fire(self, room_id: uuid.UUID, kind: str) -> None:
        self._timers.pop((room_id, kind), None)
        self._last_run[(room_id, kind)] = asyncio.get_running_loop().time()
        self.submit("tick", room_id, kind)

    # ---- workers -----------------------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            op = await self.ops.get()
            try:
                await self._run(op)
            except asyncio.CancelledError:
                raise
            except PresenceUnavailable:
                self._failed()
            except Exception as exc:  # one bad operation must never kill a worker
                log.error("presence_op_failed op=%s error_type=%s", op[0], type(exc).__name__)
            finally:
                self.ops.task_done()

    async def _run(self, op: tuple[Any, ...]) -> None:
        kind = op[0]
        if kind == "live":
            await self._op_live(op[1])
        elif kind == "touch":
            await self._op_touch(op[1])
        elif kind == "typing":
            await self._op_typing(op[1], op[2])
        elif kind == "avail":
            await self._op_avail(op[1], op[2])
        elif kind == "closed":
            await self._op_closed(op[1], op[2], op[3])
        elif kind == "tick":
            await self.tick(op[1], op[2])
        elif kind == "reregister_all":
            await self._op_reregister_all()

    def _failed(self) -> None:
        self.failures += 1
        if self.ok:
            self.ok = False
            log.warning(
                "presence_degraded redis_unavailable=true (presence is UNKNOWN, not offline)"
            )
            self.on_redis_lost()  # tell every room now

    def _succeeded(self) -> None:
        if not self.ok:
            self.ok = True
            self.recoveries += 1
            log.warning("presence_recovered: re-registering local connections")
            self.submit("reregister_all")

    async def _allowed(self, conn: Connection) -> bool:
        """Typing and availability are applied only for a connection whose access is still valid."""
        try:
            await self.hub.ensure_access(conn)
        except AlreadyClosing:
            return False
        except CloseConnection as stop:
            conn.request_close(stop.code, discard=stop.discard)
            return False
        return not conn.closing

    # ---- operations ---------------------------------------------------------------------------

    async def _op_live(self, conn: Connection) -> None:
        if conn.closing:
            return
        try:
            await self.store.register(conn.room_id, conn.user_id, conn.id)
            conn.presence_registered = True
            conn.presence_registered_at = self.hub.clock()
            self._succeeded()
            await self.store.publish_hint(conn.room_id)
        except PresenceUnavailable:
            self._failed()
        # the connection's FIRST snapshots (also when degraded: then they say so)
        await self.tick(conn.room_id, "presence", include=conn)
        await self.tick(conn.room_id, "typing", include=conn)
        self.schedule(conn.room_id, "presence")  # tell the other connections someone joined

    async def _op_touch(self, conn: Connection) -> None:
        conn.touch_pending = False
        if conn.closing:
            return
        await self.store.register(conn.room_id, conn.user_id, conn.id)
        conn.presence_registered = True
        conn.presence_registered_at = self.hub.clock()
        self._succeeded()

    async def _op_typing(self, conn: Connection, state: str) -> None:
        if not await self._allowed(conn):
            return
        if state == "start":
            await self.store.typing_start(conn.room_id, conn.user_id, conn.id)
            # Redis will not announce the 5 s expiry: look again just after it
            self.schedule(conn.room_id, "typing", after=self.settings.typing_expiry_seconds + 0.1)
        else:
            await self.store.typing_clear(conn.room_id, conn.user_id, conn.id)
        self._succeeded()
        await self.store.publish_hint(conn.room_id)
        self.schedule(conn.room_id, "typing")

    async def _op_avail(self, conn: Connection, value: str) -> None:
        if not await self._allowed(conn):
            return
        if await self.store.set_availability(conn.room_id, conn.user_id, value):  # type: ignore[arg-type]
            await self.store.publish_hint(conn.room_id)
            self.schedule(conn.room_id, "presence")
        self._succeeded()

    async def _op_closed(self, room_id: uuid.UUID, user_id: uuid.UUID, conn_id: uuid.UUID) -> None:
        await self.store.remove(room_id, user_id, conn_id)  # also clears this connection's typing
        self._succeeded()
        await self.store.publish_hint(room_id)
        if room_id in self.hub.rooms:
            self.schedule(room_id, "presence")
            self.schedule(room_id, "typing")

    async def _op_reregister_all(self) -> None:
        conns = [c for s in self.hub.rooms.values() for c in s.connections.values() if c.live]
        sem = asyncio.Semaphore(REREGISTER_CONCURRENCY)

        async def one(conn: Connection) -> None:
            async with sem:
                if conn.closing:
                    return
                try:
                    await self.store.register(conn.room_id, conn.user_id, conn.id)
                    conn.presence_registered = True
                    conn.presence_registered_at = self.hub.clock()
                except PresenceUnavailable:
                    self._failed()

        await asyncio.gather(*[one(c) for c in conns])
        for room_id in list(self.hub.rooms):
            self.schedule(room_id, "presence")
            self.schedule(room_id, "typing")

    # ---- ticks ---------------------------------------------------------------------------------

    async def tick(
        self, room_id: uuid.UUID, kind: str, *, include: Connection | None = None
    ) -> None:
        """Rebuild one snapshot and send it to the room's live local connections if it changed
        (and always to `include`)."""
        state = self.hub.rooms.get(room_id)
        if state is None:
            return
        conns = [c for c in state.connections.values() if c.live and not c.closing]
        if not conns:
            return
        self.ticks += 1
        degraded = False
        try:
            if kind == "presence":
                raw: list[Any] = await self.store.snapshot(room_id)
            else:
                raw = await self.store.typing_snapshot(room_id)
            self._succeeded()
        except PresenceUnavailable:
            self._failed()
            degraded, raw = True, []
        members = await self._members(state)
        if members is None:
            return  # cannot tell who is a member yet: send NOTHING rather than an unfiltered list
        raw = [r for r in raw if (r[0] if kind == "presence" else r) in members]
        if kind == "presence":
            signature = (tuple(sorted((str(u), a) for u, a in raw)), degraded)
            frame = protocol.presence_snapshot(room_id, list(raw), degraded)
            changed = signature != state.last_presence
            state.last_presence = signature
            self._refresh_stale_registrations(conns)
        else:
            signature = (tuple(sorted(str(u) for u in raw)), degraded)
            frame = protocol.typing_state(room_id, list(raw), degraded)
            changed = signature != state.last_typing
            state.last_typing = signature
        targets = conns if changed else ([include] if include is not None else [])
        for conn in targets:
            if conn.closing:
                continue
            (conn.outbox.set_presence if kind == "presence" else conn.outbox.set_typing)(frame)

    def _refresh_stale_registrations(self, conns: list[Connection]) -> None:
        """A flushed Redis forgets everyone; re-add connections not refreshed for a while (this
        also covers clients whose heartbeat is late) without waiting for the next heartbeat."""
        now = self.hub.clock()
        for conn in conns:
            stale = now - conn.presence_registered_at > self.settings.presence_expiry_seconds / 3
            if stale and not conn.touch_pending:
                conn.touch_pending = True
                self.submit("touch", conn)

    async def _members(self, state: RoomState) -> set[uuid.UUID] | None:
        now = self.hub.clock()
        if (
            state.members is None
            or state.members_stale
            or now - state.members_at > MEMBER_CACHE_SECONDS
        ):
            state.members_stale = False  # events seen from here on will set it again
            try:
                state.members = await realtime_access.fetch_member_ids(
                    self.hub.sessionmaker, state.room_id
                )
                state.members_at = now
            except Exception as exc:
                state.members_stale = True
                log.warning("presence_members_refresh_failed error_type=%s", type(exc).__name__)
        return state.members

    async def _tick_loop(self) -> None:
        while True:
            base = self.settings.presence_tick_seconds
            await asyncio.sleep(base * (1 + random.random() * 0.2))  # noqa: S311 - jitter
            for room_id in list(self.hub.rooms):
                try:
                    await self.tick(room_id, "presence")
                    await self.tick(room_id, "typing")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("presence_tick_failed error_type=%s", type(exc).__name__)
