"""The WebSocket wire protocol: close codes and frame builders.

WIRE FORMAT (also documented in the README)
  server -> client, always a JSON text frame:
    {"type": "ready", "schema_version": 1, "room_id": ..., "current_room_seq": N}
        "live subscription established". NOT "snapshot consistent" and NOT "replay complete".
        current_room_seq is the room's high-water mark when the subscription became live: the
        starting point for Part 5's replay.
    {"type": "room_event", "schema_version": 1, "event_id": ..., "room_id": ..., "room_seq": N,
     "event_type": ..., "resource_type": ..., "resource_id": ..., "resource_version": ...,
     "payload": {...}, "actor_id": ..., "created_at": ...}
        the same shape (minus "type") as the HTTP history endpoint's items.
  client -> server: nothing. There are no application commands and no message writes over the
  socket; any text frame closes the connection with 1008.

Clients de-duplicate by event_id and must NOT discard a lower room_seq just because a higher one
arrived: notifications can arrive out of order and more than once. Ordered recovery is Part 5.
"""

from __future__ import annotations

import uuid

from app.schemas.events import ReadyFrame, RoomEventFrame, RoomEventOut

# Close codes. 1xxx are RFC 6455 standard codes; 4xxx are application codes.
CLOSE_GOING_AWAY = 1001  # the server is shutting down
CLOSE_UNSUPPORTED_DATA = 1003  # binary frames are not supported
CLOSE_POLICY_VIOLATION = 1008  # any client text command (none exist)
CLOSE_MESSAGE_TOO_BIG = 1009  # an incoming frame larger than MAX_CLIENT_FRAME_BYTES
CLOSE_TRY_AGAIN_LATER = 1013  # the room subscription could not be established in time
CLOSE_SESSION_INVALID = 4401  # session revoked, expired, or user no longer active
CLOSE_ACCESS_REVOKED = 4403  # no longer an active member of the room
CLOSE_TOO_MANY_CONNECTIONS = 4409  # per-user or per-process connection cap reached
CLOSE_SLOW_CONSUMER = 4429  # the client is not reading fast enough

MAX_CLIENT_FRAME_BYTES = 1024

CLOSE_CODE_NAMES = {
    CLOSE_GOING_AWAY: "server_shutdown",
    CLOSE_UNSUPPORTED_DATA: "binary_not_supported",
    CLOSE_POLICY_VIOLATION: "client_commands_not_supported",
    CLOSE_MESSAGE_TOO_BIG: "frame_too_large",
    CLOSE_TRY_AGAIN_LATER: "try_again_later",
    CLOSE_SESSION_INVALID: "session_invalid",
    CLOSE_ACCESS_REVOKED: "access_revoked",
    CLOSE_TOO_MANY_CONNECTIONS: "too_many_connections",
    CLOSE_SLOW_CONSUMER: "slow_consumer",
}


def ready_frame(room_id: uuid.UUID, current_room_seq: int) -> str:
    return ReadyFrame(room_id=room_id, current_room_seq=current_room_seq).model_dump_json()


def event_frame(event: RoomEventOut) -> str:
    return RoomEventFrame(**event.model_dump()).model_dump_json()
