"""The per-process realtime hub: who is connected to which room, and what wakes them.

    Redis hint --> hub.on_room_hint(room, seq): remember the highest seq, set each connection's
                   wake Event. That is ALL a hint does (an assignment and Event.set()), so memory
                   per room is constant however many hints arrive.
    reconcile loop --> every few seconds ONE query for all rooms with local connections
                   (SELECT id, event_seq FROM rooms WHERE id = ANY(...)) finds anything a lost hint
                   hid and wakes the connections that are behind.
    connection drainer --> woken, reads PostgreSQL in order and queues frames (connection.py).

WHAT THIS REPLACES (Part 4): the notification queue + forwarding workers that pushed an event for
every notification. Nothing is pushed because a notification arrived; connections PULL from
PostgreSQL in ascending room_seq, so order, gaps and lost hints are handled in one place.

ORDERING ASSUMPTION (tested): event sequence numbers are allocated under the room lock, which is
held until commit, so COMMITTED events always form a gapless prefix 1..event_seq. A gap seen while
draining is therefore a bug, not a race: the connection gets resync_required(event_gap) and an
error is logged. It never skips.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import APP_LOGGER
from app.realtime import protocol
from app.realtime.connection import (
    AlreadyClosing,
    CloseConnection,
    Connection,
    WebSocketLike,
)
from app.realtime.subscriptions import SubscriptionManager
from app.schemas.events import RoomEventOut
from app.services import realtime_access
from app.services.realtime_access import HandshakeIdentity

log = logging.getLogger(f"{APP_LOGGER}.realtime")

STATS_INTERVAL_SECONDS = 30.0


@dataclass
class HubCounters:
    connections_accepted: int = 0
    connections_rejected_cap: int = 0
    frames_sent: int = 0
    send_timeouts: int = 0
    closed_session_invalid: int = 0
    closed_access_revoked: int = 0
    frame_rate_closes: int = 0
    watchdog_closes: int = 0
    ack_timeouts: int = 0
    window_pauses: int = 0
    replay_slot_timeouts: int = 0
    replay_peak: int = 0
    typing_dropped: int = 0
    reconcile_runs: int = 0
    reconcile_wakes: int = 0
    reconcile_failures: int = 0
    access_checks: int = 0
    hints_applied: int = 0
    closes: Counter = field(default_factory=Counter)  # by close code
    resyncs: Counter = field(default_factory=Counter)  # by reason


@dataclass
class RoomState:
    room_id: uuid.UUID
    connections: dict[uuid.UUID, Connection] = field(default_factory=dict)
    hint_seq: int = 0  # highest seq any hint named (a HINT; may be wrong)
    db_seq: int = 0  # highest event_seq read from PostgreSQL (authoritative)
    access_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Bumped by EVERY hint. A cached access verdict is only reusable while the epoch it was
    # computed under is still current: member removal commits an event, so its hint invalidates
    # the cache and the very next drain re-checks (one shared query) before sending anything.
    access_epoch: int = 0


class RealtimeHub:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        redis_client_factory: Callable[[], Redis] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.clock = clock
        self.manager = SubscriptionManager(
            settings,
            on_room_hint=self.on_room_hint,
            on_presence_hint=self.on_presence_hint,
            on_recovered=self.on_recovered,
            client_factory=redis_client_factory,
        )
        self.rooms: dict[uuid.UUID, RoomState] = {}
        self.per_user: Counter[uuid.UUID] = Counter()
        self.counters = HubCounters()
        self.replay_slots = asyncio.Semaphore(settings.replay_concurrency)
        self.replaying = 0
        self.db_fail_since: float | None = None
        self.stopping = False
        self._admitted = 0
        self._serving: set[Connection] = set()  # every admitted connection, even pre-register
        self._idle = asyncio.Event()
        self._idle.set()
        self._tasks: list[asyncio.Task[None]] = []
        self.suppress_hints = False  # TEST-ONLY fault injection: drop every incoming hint

    # ---- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Never needs Redis: the manager connects (and reconnects) in the background."""
        await self.manager.start()
        self._tasks = [
            asyncio.create_task(self._reconcile_loop(), name="ws-reconcile"),
            asyncio.create_task(self._stats_loop(), name="ws-stats"),
        ]

    async def stop(self) -> None:
        """Shutdown: close every socket with 1001, wait for them to finish, stop the loops."""
        self.stopping = True
        for conn in list(self._serving):
            conn.request_close(protocol.CLOSE_GOING_AWAY, discard=False)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._idle.wait(), timeout=self.settings.ws_send_timeout_seconds + 2
            )
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.manager.stop()

    # ---- registry and admission ----------------------------------------------------

    def all_connections(self) -> list[Connection]:
        return [c for state in self.rooms.values() for c in state.connections.values()]

    @property
    def active_connections(self) -> int:
        return self._admitted

    def admit(self, user_id: uuid.UUID) -> bool:
        return (
            self.per_user[user_id] < self.settings.ws_max_connections_per_user
            and self._admitted < self.settings.ws_max_connections_per_process
        )

    def register(self, conn: Connection) -> None:
        state = self.rooms.setdefault(conn.room_id, RoomState(conn.room_id))
        state.connections[conn.id] = conn
        conn.registered = True

    def unregister(self, conn: Connection) -> None:
        state = self.rooms.get(conn.room_id)
        if state is None or state.connections.pop(conn.id, None) is None:
            return
        conn.registered = False
        if not state.connections:
            del self.rooms[conn.room_id]

    async def authenticate(
        self, secret: str | None, room_id: uuid.UUID | None
    ) -> HandshakeIdentity | None:
        return await realtime_access.authenticate_websocket(self.sessionmaker, secret, room_id)

    async def serve(
        self, websocket: WebSocketLike, room_id: uuid.UUID, identity: HandshakeIdentity
    ) -> None:
        """Run an ALREADY-AUTHORIZED, ALREADY-ACCEPTED connection to its end."""
        user_id = identity.user_id
        if self.stopping or not self.admit(user_id):
            self.counters.connections_rejected_cap += 1
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    websocket.close(code=protocol.CLOSE_TOO_MANY_CONNECTIONS),
                    timeout=self.settings.ws_send_timeout_seconds,
                )
            return
        self._admitted += 1  # no await between admit() and this line: the caps are exact
        self.per_user[user_id] += 1
        self._idle.clear()
        self.counters.connections_accepted += 1
        conn = Connection(self, websocket, room_id, identity)
        self._serving.add(conn)
        try:
            await conn.run()
        finally:
            self._serving.discard(conn)
            self._admitted -= 1
            self.per_user[user_id] -= 1
            if self.per_user[user_id] <= 0:
                del self.per_user[user_id]
            if self._admitted == 0:
                self._idle.set()

    # ---- hints and sequence knowledge ----------------------------------------------

    def on_room_hint(self, room_id: uuid.UUID, room_seq: int) -> None:
        """Called by the Redis reader. A HINT: remember the number, wake the room. Hints for
        rooms with no local connection are ignored."""
        if self.suppress_hints:
            return
        state = self.rooms.get(room_id)
        if state is None:
            return
        self.counters.hints_applied += 1
        state.access_epoch += 1
        if room_seq > state.hint_seq:
            state.hint_seq = room_seq
        self._wake_room(state)

    def on_presence_hint(self, room_id: uuid.UUID) -> None:
        """Presence/typing changed somewhere (checkpoint 4 re-ticks the room)."""
        return None

    def on_recovered(self) -> None:
        """A Redis subscription was (re)established: hints may have been missed meanwhile, so
        every connection re-reads PostgreSQL."""
        for state in self.rooms.values():
            self._wake_room(state)

    @staticmethod
    def _wake_room(state: RoomState) -> None:
        for conn in state.connections.values():
            conn.wake.set()

    def target_seq(self, room_id: uuid.UUID) -> int:
        state = self.rooms.get(room_id)
        return max(state.hint_seq, state.db_seq) if state else 0

    def authoritative_seq(self, room_id: uuid.UUID) -> int:
        state = self.rooms.get(room_id)
        return state.db_seq if state else 0

    def discard_hint(self, room_id: uuid.UUID) -> None:
        state = self.rooms.get(room_id)
        if state is not None:
            state.hint_seq = 0

    async def read_head(self, room_id: uuid.UUID) -> int:
        """H: the room's event_seq right now (handshake step 6)."""
        head = await self.db(realtime_access.read_event_seq, self.sessionmaker, room_id)
        state = self.rooms.get(room_id)
        if state is not None and head > state.db_seq:
            state.db_seq = head
        return head

    def note_events(self, room_id: uuid.UUID, events: list[RoomEventOut]) -> None:
        """Called after a connection queued events (checkpoint 4 uses it for member changes)."""
        return None

    # ---- database access with a grace period -----------------------------------------

    async def db(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a read. If PostgreSQL is unavailable, retry with backoff for up to
        ACCESS_DB_GRACE_SECONDS (nothing protected is sent meanwhile, because every caller is
        BEFORE the send), then close the calling connection with 1013."""
        delay = 0.1
        while True:
            try:
                result = await fn(*args)
            except asyncio.CancelledError:
                raise
            except (SQLAlchemyError, OSError) as exc:
                now = self.clock()
                if self.db_fail_since is None:
                    self.db_fail_since = now
                    log.warning("ws_database_unavailable error_type=%s", type(exc).__name__)
                if now - self.db_fail_since >= self.settings.access_db_grace_seconds:
                    raise CloseConnection(
                        protocol.CLOSE_TRY_AGAIN_LATER, reason="database_unavailable"
                    ) from None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
            else:
                self.db_fail_since = None
                return result

    # ---- access checks (one query per room, cached for ACCESS_CACHE_SECONDS) ---------

    async def ensure_access(self, conn: Connection) -> None:
        """Raise unless this connection's session is valid and its user is still an active
        member. ONE query serves every local connection of the room; the result is reused for
        ACCESS_CACHE_SECONDS. Connections that fail are closed (4401/4403) with their pending
        frames discarded."""
        if conn.closing:
            raise AlreadyClosing
        state = self.rooms.get(conn.room_id)
        if state is None:
            raise AlreadyClosing
        if self._access_fresh(conn, state):
            return
        async with state.access_lock:
            if conn.closing:
                raise AlreadyClosing
            if not self._access_fresh(conn, state):  # another connection may have just checked
                await self._check_room_access(state)
        if conn.closing:
            raise AlreadyClosing

    def _access_fresh(self, conn: Connection, state: RoomState) -> bool:
        return (
            self.clock() - conn.access_checked_at < self.settings.access_cache_seconds
            and conn.access_epoch == state.access_epoch
        )

    async def _check_room_access(self, state: RoomState) -> None:
        conns = [c for c in state.connections.values() if not c.closing]
        if not conns:
            return
        started, epoch = self.clock(), state.access_epoch
        self.counters.access_checks += 1
        verdicts = await self.db(
            realtime_access.check_access,
            self.sessionmaker,
            state.room_id,
            list({c.session_id for c in conns}),
        )
        for conn in conns:
            verdict = verdicts.get(conn.session_id)
            if verdict is None or not verdict.session_ok:
                self.counters.closed_session_invalid += 1
                conn.request_close(protocol.CLOSE_SESSION_INVALID, discard=True)
            elif not verdict.member_ok:
                self.counters.closed_access_revoked += 1
                conn.request_close(protocol.CLOSE_ACCESS_REVOKED, discard=True)
            else:
                conn.access_checked_at = started
                conn.access_epoch = epoch

    # ---- presence hooks (implemented in checkpoint 4) -------------------------------

    def touch_presence(self, conn: Connection) -> None:
        return None

    def submit_typing(self, conn: Connection, state: str) -> None:
        return None

    def submit_availability(self, conn: Connection, value: str) -> None:
        return None

    async def on_connection_live(self, conn: Connection) -> None:
        return None

    async def on_connection_closed(self, conn: Connection) -> None:
        return None

    # ---- reconciliation: find what a lost hint hid ------------------------------------

    async def _reconcile_loop(self) -> None:
        while True:
            s = self.settings
            base = (
                s.reconcile_interval_seconds
                if self.manager.healthy
                else s.reconcile_interval_degraded_seconds
            )
            await asyncio.sleep(base * (1 + random.random() * 0.2))  # noqa: S311 - jitter
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.counters.reconcile_failures += 1
                log.warning("ws_reconcile_failed error_type=%s", type(exc).__name__)

    async def reconcile_once(self) -> int:
        """ONE query for every room that has local connections; wake the connections that are
        behind. Returns how many connections were woken."""
        room_ids = list(self.rooms)
        if not room_ids:
            return 0
        seqs = await realtime_access.read_event_seqs(self.sessionmaker, room_ids)
        self.counters.reconcile_runs += 1
        woken = 0
        for room_id, seq in seqs.items():
            state = self.rooms.get(room_id)
            if state is None:
                continue
            state.db_seq = seq
            if state.hint_seq <= seq:
                state.hint_seq = 0  # the hint (if any) is covered by what the database says
            for conn in state.connections.values():
                if conn.live and conn.last_sent < max(state.hint_seq, state.db_seq):
                    conn.wake.set()
                    woken += 1
        self.counters.reconcile_wakes += woken
        return woken

    # ---- observability ----------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "active_connections": self.active_connections,
            "rooms_with_connections": len(self.rooms),
            "replaying": self.replaying,
            "redis": self.manager.status(),
            **{k: v for k, v in self.counters.__dict__.items()},
        }

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_SECONDS)
            s = self.stats()
            redis = s["redis"]
            log.info(
                "realtime_stats connections=%s rooms=%s replaying=%s redis_healthy=%s "
                "subscribed_rooms=%s hints=%s reconcile_wakes=%s resyncs=%s closes=%s "
                "frames_sent=%s",
                s["active_connections"],
                s["rooms_with_connections"],
                s["replaying"],
                redis["healthy"],
                redis["subscribed_rooms"],
                redis["hints_received"],
                s["reconcile_wakes"],
                dict(s["resyncs"]),
                dict(s["closes"]),
                s["frames_sent"],
            )
