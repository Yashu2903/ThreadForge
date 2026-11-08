"""Parsing the notifications that arrive from Redis.

Redis is a hint channel, so everything arriving from it is treated as untrusted input: it is size
limited, must be JSON, must match the strict envelope schema, and must name the same room as the
channel it arrived on. Anything else is dropped (and counted), never forwarded.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from app.realtime.channels import room_id_from_channel
from app.schemas.events import OutboxEnvelope

MAX_NOTIFICATION_BYTES = 1024


class NotificationRejected(Exception):
    """A notification was dropped; `reason` is a short safe label used for counters and logs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Notification:
    room_id: uuid.UUID
    event_id: uuid.UUID
    room_seq: int


def parse_notification(prefix: str, channel: str, data: str | bytes) -> Notification:
    """Validate one pub/sub message. Raises NotificationRejected (never any other error)."""
    raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
    if len(raw) > MAX_NOTIFICATION_BYTES:
        raise NotificationRejected("oversized")
    channel_room = room_id_from_channel(prefix, channel)
    if channel_room is None:
        raise NotificationRejected("bad_channel")
    try:
        decoded = json.loads(raw)
    except (ValueError, RecursionError):
        raise NotificationRejected("not_json") from None
    try:
        envelope = OutboxEnvelope.model_validate(decoded)
    except ValidationError:
        raise NotificationRejected("schema") from None
    if envelope.room_id != channel_room:
        raise NotificationRejected("channel_mismatch")  # an event for room A on room B's channel
    return Notification(
        room_id=envelope.room_id, event_id=envelope.event_id, room_seq=envelope.room_seq
    )
