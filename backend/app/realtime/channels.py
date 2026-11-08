"""Redis channel naming, shared by the dispatcher (publisher) and the API (subscribers).

One channel per room: {REDIS_CHANNEL_PREFIX}:room:{room_id}. Subscribers use explicit
SUBSCRIBE/UNSUBSCRIBE per room (never PSUBSCRIBE of all rooms), so an API process receives only
the rooms it actually serves.
"""

from __future__ import annotations

import uuid


def room_channel(prefix: str, room_id: uuid.UUID) -> str:
    return f"{prefix}:room:{room_id}"


def room_id_from_channel(prefix: str, channel: str) -> uuid.UUID | None:
    """The room id a channel name refers to, or None if it is not one of ours."""
    head = f"{prefix}:room:"
    if not channel.startswith(head):
        return None
    try:
        room_id = uuid.UUID(channel[len(head) :])
    except ValueError:
        return None
    return room_id if channel == room_channel(prefix, room_id) else None
