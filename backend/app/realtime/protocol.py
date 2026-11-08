"""The WebSocket wire protocol (schema_version 1): close codes, client frames, server frames.

CONNECTION FLOW
  client                                              server
  ------                                              ------
  (HTTP) GET /rooms/{id}/sync  ->  state + through_seq (one database snapshot)
  open ws /rooms/{id}/ws  (Cookie + Origin; any failure is a bare HTTP 403 before accept)
  {"type":"resume","schema_version":1,"after_seq":N}  ->   (must be first, within RESUME_TIMEOUT)
                              <-  {"type":"sync_started", ... after_seq, through_seq}
                              <-  {"type":"room_event", ...}  (ascending room_seq)
                              <-  {"type":"sync_complete","through_seq":H}
                              <-  presence_snapshot, typing_state
                              <-  room_event ... (live, still ascending, read from PostgreSQL)
  {"type":"ack","through_seq":K}          (flow control + diagnostics; NOT a read receipt)
  {"type":"heartbeat"}  (every 15 s)                  <-  {"type":"heartbeat_ack","server_time":...}
  {"type":"typing","state":"start"|"stop"}
  {"type":"set_availability","value":"available"|"busy"|"dnd"}

Client frames are JSON text of at most 1 KiB with NO unknown fields and no room or user identity
(identity always comes from the authenticated connection). A frame that does not validate closes
the connection with 4400. Messages are never written over the socket: they stay HTTP.

`sync_complete` (not the socket opening) means "synchronized". room_event frames are the Part 4
event shape. A duplicate is possible and is ignored by the client by room_seq.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.events import RoomEventFrame, RoomEventOut

SCHEMA_VERSION = 1
MAX_BIGINT = 2**63 - 1

# ---- close codes (one table for Parts 4 and 5; client reactions are in the README) --------------
CLOSE_GOING_AWAY = 1001  # server shutting down                      -> reconnect with backoff
CLOSE_UNSUPPORTED_DATA = 1003  # binary frame                        -> terminal protocol bug
CLOSE_MESSAGE_TOO_BIG = 1009  # frame larger than 1 KiB              -> terminal protocol bug
CLOSE_TRY_AGAIN_LATER = 1013  # overload / database unavailable too long -> reconnect with backoff
CLOSE_PROTOCOL_VIOLATION = 4400  # bad/unknown frame, invalid ack, unsupported version -> terminal
CLOSE_SESSION_INVALID = 4401  # session revoked/expired/user inactive -> terminal; sign in again
CLOSE_ACCESS_REVOKED = 4403  # not (or no longer) an active member -> terminal; clear room data
CLOSE_TIMEOUT = 4408  # resume or heartbeat timeout                  -> reconnect with backoff
CLOSE_TOO_MANY_CONNECTIONS = (
    4409  # per-user / per-process cap     -> retry after >= 10 s, stop after 3
)
CLOSE_RESYNC_REQUIRED = 4410  # cursor cannot be served             -> fetch bootstrap, reconnect
CLOSE_SLOW_CONSUMER = 4429  # slow consumer or frame rate exceeded  -> reconnect with backoff

CLOSE_CODE_NAMES = {
    CLOSE_GOING_AWAY: "server_shutdown",
    CLOSE_UNSUPPORTED_DATA: "binary_not_supported",
    CLOSE_MESSAGE_TOO_BIG: "frame_too_large",
    CLOSE_TRY_AGAIN_LATER: "try_again_later",
    CLOSE_PROTOCOL_VIOLATION: "protocol_violation",
    CLOSE_SESSION_INVALID: "session_invalid",
    CLOSE_ACCESS_REVOKED: "access_revoked",
    CLOSE_TIMEOUT: "timeout",
    CLOSE_TOO_MANY_CONNECTIONS: "too_many_connections",
    CLOSE_RESYNC_REQUIRED: "resync_required",
    CLOSE_SLOW_CONSUMER: "slow_consumer",
}

MAX_CLIENT_FRAME_BYTES = 1024


# ---- client -> server ---------------------------------------------------------------------------
class _Client(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResumeFrame(_Client):
    type: Literal["resume"]
    schema_version: Literal[1]
    after_seq: int = Field(ge=0, le=MAX_BIGINT)


class AckFrame(_Client):
    type: Literal["ack"]
    through_seq: int = Field(ge=0, le=MAX_BIGINT)


class HeartbeatFrame(_Client):
    type: Literal["heartbeat"]


class TypingFrame(_Client):
    type: Literal["typing"]
    state: Literal["start", "stop"]


class SetAvailabilityFrame(_Client):
    type: Literal["set_availability"]
    value: Literal["available", "busy", "dnd"]


ClientFrame = ResumeFrame | AckFrame | HeartbeatFrame | TypingFrame | SetAvailabilityFrame
_CLIENT_MODELS: dict[str, type[_Client]] = {
    "resume": ResumeFrame,
    "ack": AckFrame,
    "heartbeat": HeartbeatFrame,
    "typing": TypingFrame,
    "set_availability": SetAvailabilityFrame,
}


class FrameError(Exception):
    """A client frame that must close the connection with `code` (never echoes the content)."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def parse_client_text(text: str) -> ClientFrame:
    """Validate one client text frame. Raises FrameError (1009 too large, 4400 anything else)."""
    if len(text.encode("utf-8", errors="replace")) > MAX_CLIENT_FRAME_BYTES:
        raise FrameError(CLOSE_MESSAGE_TOO_BIG, "frame_too_large")
    try:
        decoded: Any = json.loads(text)
    except (ValueError, RecursionError):
        raise FrameError(CLOSE_PROTOCOL_VIOLATION, "not_json") from None
    kind = decoded.get("type") if isinstance(decoded, dict) else None
    model = _CLIENT_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise FrameError(CLOSE_PROTOCOL_VIOLATION, "unknown_frame")
    try:
        return model.model_validate(decoded)  # type: ignore[return-value]
    except ValidationError:
        raise FrameError(CLOSE_PROTOCOL_VIOLATION, f"invalid_{kind}") from None


# ---- server -> client ---------------------------------------------------------------------------
class _Server(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SyncStartedFrame(_Server):
    type: Literal["sync_started"] = "sync_started"
    schema_version: int = SCHEMA_VERSION
    room_id: uuid.UUID
    after_seq: int
    through_seq: int


class SyncCompleteFrame(_Server):
    type: Literal["sync_complete"] = "sync_complete"
    through_seq: int


class HeartbeatAckFrame(_Server):
    type: Literal["heartbeat_ack"] = "heartbeat_ack"
    server_time: datetime


class PresenceEntry(_Server):
    user_id: uuid.UUID
    availability: Literal["available", "busy", "dnd"]


class PresenceSnapshotFrame(_Server):
    type: Literal["presence_snapshot"] = "presence_snapshot"
    room_id: uuid.UUID
    online: list[PresenceEntry]
    degraded: bool


class TypingStateFrame(_Server):
    type: Literal["typing_state"] = "typing_state"
    room_id: uuid.UUID
    typing: list[uuid.UUID]
    degraded: bool


ResyncReason = Literal["cursor_ahead", "too_far_behind", "event_gap"]


class ResyncRequiredFrame(_Server):
    type: Literal["resync_required"] = "resync_required"
    reason: ResyncReason


def dump(frame: BaseModel) -> str:
    return frame.model_dump_json()


def sync_started(room_id: uuid.UUID, after_seq: int, through_seq: int) -> str:
    return dump(SyncStartedFrame(room_id=room_id, after_seq=after_seq, through_seq=through_seq))


def sync_complete(through_seq: int) -> str:
    return dump(SyncCompleteFrame(through_seq=through_seq))


def heartbeat_ack() -> str:
    return dump(HeartbeatAckFrame(server_time=datetime.now(UTC)))


def presence_snapshot(
    room_id: uuid.UUID, online: list[tuple[uuid.UUID, str]], degraded: bool
) -> str:
    entries = [PresenceEntry(user_id=u, availability=a) for u, a in online]
    return dump(PresenceSnapshotFrame(room_id=room_id, online=entries, degraded=degraded))


def typing_state(room_id: uuid.UUID, typing: list[uuid.UUID], degraded: bool) -> str:
    return dump(TypingStateFrame(room_id=room_id, typing=typing, degraded=degraded))


def resync_required(reason: ResyncReason) -> str:
    return dump(ResyncRequiredFrame(reason=reason))


def event_frame(event: RoomEventOut) -> str:
    """The Part 4 room_event shape, unchanged."""
    return RoomEventFrame(**event.model_dump()).model_dump_json()


# Annotated alias kept for documentation tools.
ServerFrameType = Annotated[str, "one JSON text frame"]
