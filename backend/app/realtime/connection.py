"""One WebSocket connection: four cooperating tasks around one outgoing queue.

    receiver  reads client frames (resume was already consumed), validates them, rate-limits them,
              and NEVER waits on anything but the socket, so acks and heartbeats keep flowing even
              while the drainer is paused or the database is slow.
    drainer   (`_pipeline` + `_drain`) the ONLY producer of durable event frames. Pull-based: it
              reads the next page of room_events from PostgreSQL by primary-key range, checks it
              is gapless, queues it, and repeats until caught up. It then sleeps until woken by a
              hint or by reconciliation. It holds only last_sent / last_acked in memory.
    writer    the ONLY task that writes to the socket (including the close frame). Every send has
              a timeout, so a stalled TCP peer cannot block anything but its own connection.
    watchdog  closes 4408 when no client frame of any kind has arrived within the timeout.

If one of them ends (the writer after a close, the receiver after a disconnect, or an unexpected
error), `run` cancels the others and cleans up in `finally`: unregister from the hub, give back the
Redis subscription reference, remove presence/typing entries, close the socket.

STATE: PostgreSQL holds the events. This object holds the cursor-in-flight (last_sent, last_acked)
and a bounded queue; the client holds the real cursor (what it has fully processed).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from app.config import Settings
from app.logging_config import APP_LOGGER
from app.realtime import protocol
from app.realtime.protocol import (
    AckFrame,
    FrameError,
    HeartbeatFrame,
    ResumeFrame,
    SetAvailabilityFrame,
    TypingFrame,
    parse_client_text,
)
from app.services import realtime_access
from app.services.realtime_access import HandshakeIdentity

if TYPE_CHECKING:
    from app.realtime.room_hub import RealtimeHub

log = logging.getLogger(f"{APP_LOGGER}.realtime")

CONTROL_HEADROOM = 16  # heartbeat acks may exceed the queue bound by this much, then are dropped


class WebSocketLike(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...

    async def receive(self) -> dict[str, Any]: ...


class CloseConnection(Exception):
    """Close THIS connection with `code`. `frame` (e.g. resync_required) is delivered first;
    `discard` drops everything still queued (access loss: nothing more may be sent)."""

    def __init__(
        self, code: int, *, frame: str | None = None, discard: bool = True, reason: str = ""
    ) -> None:
        super().__init__(reason or str(code))
        self.code = code
        self.frame = frame
        self.discard = discard


class AlreadyClosing(Exception):
    """The connection was closed by someone else (the hub) while this task was working."""


class RateLimit:
    """Token bucket: `rate` tokens per second, up to `burst` stored."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float]) -> None:
        self.rate, self.burst, self._clock = rate, burst, clock
        self.tokens = burst
        self._last = clock()

    def allow(self) -> bool:
        now = self._clock()
        self.tokens = min(self.burst, self.tokens + (now - self._last) * self.rate)
        self._last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Outbox:
    """The bounded outgoing queue. Durable and control frames keep their order in `ordered`;
    presence_snapshot and typing_state are COALESCED (only the newest of each is kept), so a burst
    of presence changes can never grow memory or delay an event."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self.ordered: deque[str] = deque()
        self.presence: str | None = None
        self.typing: str | None = None
        self.has_work = asyncio.Event()
        self.space = asyncio.Event()
        self.closing = False

    async def put(self, frame: str) -> bool:
        """Queue an ordered frame, waiting for space (backpressure on the drainer). False if the
        connection is closing (the frame is discarded)."""
        while len(self.ordered) >= self.maxsize and not self.closing:
            self.space.clear()
            await self.space.wait()
        if self.closing:
            return False
        self.ordered.append(frame)
        self.has_work.set()
        return True

    def try_put(self, frame: str) -> bool:
        """Non-blocking (control frames from the receiver). Dropped when far over the bound."""
        if self.closing or len(self.ordered) >= self.maxsize + CONTROL_HEADROOM:
            return False
        self.ordered.append(frame)
        self.has_work.set()
        return True

    def set_presence(self, frame: str) -> None:
        if not self.closing:
            self.presence = frame
            self.has_work.set()

    def set_typing(self, frame: str) -> None:
        if not self.closing:
            self.typing = frame
            self.has_work.set()

    def clear(self) -> None:
        self.ordered.clear()
        self.presence = self.typing = None
        self.space.set()

    def start_close(self, final_frame: str | None) -> None:
        if final_frame is not None:
            self.ordered.append(final_frame)  # the close frame always follows it
        self.closing = True
        self.has_work.set()
        self.space.set()

    def next(self) -> str | None:
        if self.ordered:
            frame = self.ordered.popleft()
            self.space.set()
            return frame
        if self.presence is not None:
            frame, self.presence = self.presence, None
            return frame
        if self.typing is not None:
            frame, self.typing = self.typing, None
            return frame
        return None

    def __len__(self) -> int:
        return len(self.ordered) + (self.presence is not None) + (self.typing is not None)


class Connection:
    def __init__(
        self,
        hub: RealtimeHub,
        websocket: WebSocketLike,
        room_id: uuid.UUID,
        identity: HandshakeIdentity,
    ) -> None:
        self.hub = hub
        self.settings: Settings = hub.settings
        self.websocket = websocket
        self.room_id = room_id
        self.user_id = identity.user_id
        self.session_id = identity.session_id
        self.id = uuid.uuid4()  # server-generated; never sent to clients
        self.clock = hub.clock
        s = self.settings
        self.outbox = Outbox(s.ws_connection_queue_size)
        self.resume_after = 0
        self.last_sent = 0  # highest room_seq QUEUED for this connection
        self.last_acked = 0  # highest room_seq the client reports having processed
        self.wake = asyncio.Event()  # set by hints, reconciliation and recovery
        self.ack_event = asyncio.Event()
        self.live = False
        self.degraded = False  # no Redis subscription for this room (durable delivery continues)
        self.closing = False
        self.close_code: int | None = None
        self.close_sent = False
        self.client_gone = False
        self.access_checked_at = float("-inf")
        self.access_epoch = -1
        self.last_frame_at = self.clock()
        self.has_ref = False  # holds a Redis subscription reference
        self.registered = False
        self.writer_done = asyncio.Event()
        self._frames = RateLimit(
            s.ws_client_frames_per_second, 2 * s.ws_client_frames_per_second, self.clock
        )
        self._acks = RateLimit(s.ws_ack_frames_per_second, s.ws_ack_frames_per_second, self.clock)
        self._typing = RateLimit(
            s.ws_typing_frames_per_second, s.ws_typing_frames_per_second, self.clock
        )

    # ---- lifecycle ---------------------------------------------------------------

    async def run(self) -> None:
        try:
            resume = await self._await_resume()
            if resume is None:
                return
            self.resume_after = self.last_sent = self.last_acked = resume.after_seq
            self.last_frame_at = self.clock()
            tasks = [
                asyncio.create_task(self._writer(), name=f"ws-writer-{self.id}"),
                asyncio.create_task(self._receiver(), name=f"ws-receiver-{self.id}"),
                asyncio.create_task(self._watchdog(), name=f"ws-watchdog-{self.id}"),
                asyncio.create_task(self._pipeline(), name=f"ws-drainer-{self.id}"),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        log.error(
                            "ws_task_failed room_id=%s task=%s error_type=%s",
                            self.room_id,
                            task.get_name().split("-")[1],
                            type(task.exception()).__name__,
                        )
                        self.close_code = self.close_code or protocol.CLOSE_TRY_AGAIN_LATER
            finally:  # structured: nothing outlives the connection
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        self.closing = True
        self.live = False
        self.outbox.clear()
        self.outbox.closing = True
        self.hub.unregister(self)
        if self.has_ref:
            self.has_ref = False
            self.hub.manager.release_nowait(self.room_id)
        with contextlib.suppress(Exception):
            await self.hub.on_connection_closed(self)
        if not self.close_sent and not self.client_gone:
            await self._send_close(self.close_code if self.close_code is not None else 1000)

    async def _send_close(self, code: int) -> None:
        self.close_sent = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.websocket.close(code=code), timeout=self.settings.ws_send_timeout_seconds
            )

    def request_close(self, code: int, *, frame: str | None = None, discard: bool = True) -> None:
        """Stop this connection (callable from any task or the hub, never blocks). The writer
        sends `frame` (if any) and then the close frame; with discard=True nothing else is sent."""
        if self.closing:
            return
        self.closing = True
        self.close_code = code
        self.live = False
        if discard:
            self.outbox.clear()
        self.outbox.start_close(frame)
        self.hub.unregister(self)  # no more wake-ups, access checks or presence for this one
        self.hub.counters.closes[code] += 1
        self.wake.set()
        self.ack_event.set()

    async def _park(self) -> None:
        """Tasks that must not end a healthy connection wait here once a close was requested, so
        the writer can flush the final frame and the close frame first."""
        await self.writer_done.wait()

    # ---- handshake: the first frame ---------------------------------------------

    async def _await_resume(self) -> ResumeFrame | None:
        try:
            message = await asyncio.wait_for(
                self.websocket.receive(), timeout=self.settings.ws_resume_timeout_seconds
            )
        except TimeoutError:
            self.close_code = protocol.CLOSE_TIMEOUT
            return None
        except Exception:
            self.client_gone = True
            return None
        if message.get("type") == "websocket.disconnect":
            self.client_gone = True
            return None
        if message.get("bytes") is not None:
            self.close_code = protocol.CLOSE_UNSUPPORTED_DATA
            return None
        try:
            frame = parse_client_text(message.get("text") or "")
        except FrameError as exc:
            self.close_code = exc.code
            return None
        if not isinstance(frame, ResumeFrame):
            self.close_code = protocol.CLOSE_PROTOCOL_VIOLATION  # resume must come first
            return None
        return frame

    # ---- writer ------------------------------------------------------------------

    async def _writer(self) -> None:
        outbox, timeout = self.outbox, self.settings.ws_send_timeout_seconds
        try:
            while True:
                frame = outbox.next()
                if frame is None:
                    if outbox.closing:
                        await self._send_close(
                            self.close_code if self.close_code is not None else 1000
                        )
                        return
                    outbox.has_work.clear()
                    await outbox.has_work.wait()
                    continue
                try:
                    await asyncio.wait_for(self.websocket.send_text(frame), timeout=timeout)
                except TimeoutError:
                    self.hub.counters.send_timeouts += 1
                    log.warning("ws_send_timeout room_id=%s connection=%s", self.room_id, self.id)
                    self.request_close(protocol.CLOSE_SLOW_CONSUMER)
                    continue
                except Exception:  # the peer went away mid-send
                    self.client_gone = True
                    return
                self.hub.counters.frames_sent += 1
        finally:
            self.writer_done.set()

    # ---- receiver ----------------------------------------------------------------

    async def _receiver(self) -> None:
        while not self.closing:
            try:
                message = await self.websocket.receive()
            except Exception:
                self.client_gone = True
                return
            if message.get("type") == "websocket.disconnect":
                self.client_gone = True
                return
            self.last_frame_at = self.clock()  # ANY frame proves the peer is alive
            if self.closing:
                continue
            if message.get("bytes") is not None:
                self.request_close(protocol.CLOSE_UNSUPPORTED_DATA)
                continue
            text = message.get("text")
            if text is None:
                continue
            if not self._frames.allow():
                self.hub.counters.frame_rate_closes += 1
                self.request_close(protocol.CLOSE_SLOW_CONSUMER)
                continue
            try:
                frame = parse_client_text(text)
            except FrameError as exc:
                self.request_close(exc.code)
                continue
            self._handle_frame(frame)
        await self._park()

    def _handle_frame(self, frame: object) -> None:
        if isinstance(frame, AckFrame):
            self._handle_ack(frame.through_seq)
        elif isinstance(frame, HeartbeatFrame):
            self.outbox.try_put(protocol.heartbeat_ack())
            self.hub.touch_presence(self)
        elif isinstance(frame, TypingFrame):
            if not self.live or not self._typing.allow():
                self.hub.counters.typing_dropped += 1
            else:
                self.hub.submit_typing(self, frame.state)
        elif isinstance(frame, SetAvailabilityFrame):
            if self.live:
                self.hub.submit_availability(self, frame.value)
        else:  # a second resume, or anything else
            self.request_close(protocol.CLOSE_PROTOCOL_VIOLATION)

    def _handle_ack(self, through_seq: int) -> None:
        """Acknowledgements drive flow control and diagnostics ONLY. They are not read receipts
        and have no durable effect: the reconnect cursor lives in the client."""
        if not self._acks.allow():
            self.hub.counters.frame_rate_closes += 1
            self.request_close(protocol.CLOSE_SLOW_CONSUMER)
        elif through_seq < self.last_acked or through_seq > self.last_sent:
            # below the accepted resume point or going backwards, or acknowledging an event we
            # never sent: a client bug (or an attack), never a reason to guess
            self.request_close(protocol.CLOSE_PROTOCOL_VIOLATION)
        elif through_seq > self.last_acked:  # equal is ignored
            self.last_acked = through_seq
            self.ack_event.set()

    # ---- watchdog ----------------------------------------------------------------

    async def _watchdog(self) -> None:
        timeout = self.settings.ws_watchdog_timeout_seconds
        interval = max(0.05, min(1.0, timeout / 10))
        while not self.closing:
            await asyncio.sleep(interval)
            if self.clock() - self.last_frame_at > timeout:
                self.hub.counters.watchdog_closes += 1
                self.request_close(protocol.CLOSE_TIMEOUT)
        await self._park()

    # ---- the drainer: handshake steps 3-11 and the live loop ---------------------

    async def _pipeline(self) -> None:
        hub, s = self.hub, self.settings
        slot = False
        try:
            try:
                await asyncio.wait_for(
                    hub.replay_slots.acquire(), timeout=s.replay_slot_wait_seconds
                )
            except TimeoutError:
                hub.counters.replay_slot_timeouts += 1
                raise CloseConnection(
                    protocol.CLOSE_TRY_AGAIN_LATER, reason="replay_slot"
                ) from None
            slot = True
            hub.replaying += 1
            hub.counters.replay_peak = max(hub.counters.replay_peak, hub.replaying)
            hub.register(self)  # 4. registered with the room hub
            self.has_ref = True
            self.degraded = not await hub.manager.acquire(self.room_id)  # 5. never fatal
            head = await self.hub.read_head(self.room_id)  # 6. H, AFTER the subscription step
            after = self.resume_after
            if after > head:  # 7. cursors that cannot be served
                raise self._resync("cursor_ahead")
            if head - after > s.replay_max_events:
                raise self._resync("too_far_behind")
            await self.outbox.put(protocol.sync_started(self.room_id, after, head))  # 8
            await self._drain(upto=head)  # 9. replay (after, head]
            if self.closing:
                raise AlreadyClosing
            await self.outbox.put(protocol.sync_complete(head))  # 10
            slot = False
            hub.replaying -= 1
            hub.replay_slots.release()
            self.live = True
            await hub.on_connection_live(self)  # presence registration + first snapshots
            while not self.closing:  # 11. one more pass (events after H), then wait for wake-ups
                self.wake.clear()
                await self._drain(upto=None)
                await self.wake.wait()
        except CloseConnection as stop:
            self.request_close(stop.code, frame=stop.frame, discard=stop.discard)
        except AlreadyClosing:
            pass
        finally:
            if slot:
                hub.replaying -= 1
                hub.replay_slots.release()
        await self._park()

    def _resync(self, reason: protocol.ResyncReason) -> CloseConnection:
        self.hub.counters.resyncs[reason] += 1
        return CloseConnection(
            protocol.CLOSE_RESYNC_REQUIRED,
            frame=protocol.resync_required(reason),
            discard=False,
            reason=reason,
        )

    async def _db(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await self.hub.db(fn, *args)

    async def _wait_window(self) -> None:
        """Flow control: pause while too many events are unacknowledged. The receiver keeps
        running, so the ack that ends the pause (and heartbeats) are still read. If acks make no
        progress for ACK_TIMEOUT the client is not keeping up: 4429."""
        s = self.settings
        if self.last_sent - self.last_acked < s.ack_window:
            return
        self.hub.counters.window_pauses += 1
        progress_at, seen = self.clock(), self.last_acked
        while self.last_sent - self.last_acked >= s.ack_window and not self.closing:
            self.ack_event.clear()
            remaining = s.ack_timeout_seconds - (self.clock() - progress_at)
            try:
                await asyncio.wait_for(self.ack_event.wait(), timeout=max(remaining, 0.001))
            except TimeoutError:
                self.hub.counters.ack_timeouts += 1
                raise CloseConnection(protocol.CLOSE_SLOW_CONSUMER, reason="ack_timeout") from None
            if self.last_acked != seen:
                progress_at, seen = self.clock(), self.last_acked

    async def _drain(self, *, upto: int | None) -> None:
        """Send events in ascending room_seq from last_sent: up to `upto` (replay) or as far as
        PostgreSQL has (live). Never skips: a missing sequence number is a bug (event_gap)."""
        hub, s, room = self.hub, self.settings, self.room_id
        while not self.closing:
            await self._wait_window()
            await hub.ensure_access(self)  # raises CloseConnection / AlreadyClosing
            if self.closing:
                return
            target = upto if upto is not None else hub.target_seq(room)
            if self.last_sent >= target:
                return
            limit = (
                s.replay_page_size
                if upto is None
                else min(s.replay_page_size, upto - self.last_sent)
            )
            # never read past the ack window: unacknowledged events stay <= ACK_WINDOW exactly
            limit = min(limit, max(1, s.ack_window - (self.last_sent - self.last_acked)))
            events = await self._db(
                realtime_access.fetch_events_page, hub.sessionmaker, room, self.last_sent, limit
            )  # the database session is closed before anything is sent
            if self.closing:
                return
            if not events:
                if upto is None and hub.authoritative_seq(room) <= self.last_sent:
                    hub.discard_hint(room)  # a hint ahead of the database: ignore it
                    return
                raise self._gap(self.last_sent + 1, None)
            expected = self.last_sent + 1
            for event in events:
                if event.room_seq != expected:
                    raise self._gap(expected, event.room_seq)
                expected += 1
            for event in events:
                if not await self.outbox.put(protocol.event_frame(event)):
                    return
                self.last_sent = event.room_seq
            hub.note_events(room, events)

    def _gap(self, expected: int, got: int | None) -> CloseConnection:
        log.error(
            "event_gap room_id=%s expected_seq=%s found_seq=%s (committed events must be gapless)",
            self.room_id,
            expected,
            got,
        )
        return self._resync("event_gap")
