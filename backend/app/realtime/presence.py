"""Presence and typing: ephemeral state in Redis, never in PostgreSQL.

WHAT LIVES WHERE
  Redis (per room, every key has a TTL):
    {prefix}:presence:{room}:conns  sorted set. member "{user_id}:{conn_id}", score = expiry in
                                    milliseconds. conn_id is a random server-side value that is
                                    never sent to clients. No IPs, no user agents.
    {prefix}:presence:{room}:avail  hash, user_id -> available | busy | dnd (shared by that user's
                                    tabs; last write wins; per room and per user, NOT global).
    {prefix}:typing:{room}          sorted set, member "{user_id}:{conn_id}", score = expiry (ms).
  PostgreSQL / room_events / outbox: nothing. Presence and typing never touch them.

WHY LUA. Each operation (add a connection AND drop expired ones AND reset availability if the user
was offline) must be ONE atomic step, or two API processes interleaving could resurrect a user
who just left. A Lua script runs atomically inside Redis, and it reads Redis's own clock with
TIME, so API servers' clocks do not matter. TIME inside a script that then writes is allowed with
effects replication, the default since Redis 5 and the only mode in 7 (redis.io "Scripting with
Lua"). Every key a script touches is passed in KEYS (required for Redis Cluster; this deployment
uses one instance, and the two keys of one room are the only ones a single script touches).

Presence is a SNAPSHOT: a full list of online users (a room has at most 50 members). A user is
online while any of their connections has an unexpired entry; entries expire on their own, which
Redis never announces, so each API process re-reads on a timer (hub.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Literal

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis

from app.config import Settings
from app.logging_config import APP_LOGGER
from app.realtime.channels import presence_hint_channel
from app.realtime.publisher import create_redis_client

log = logging.getLogger(f"{APP_LOGGER}.realtime")

Availability = Literal["available", "busy", "dnd"]
KEY_TTL_PADDING_SECONDS = 60  # a key outlives its newest entry by this much, then vanishes


class PresenceUnavailable(Exception):
    """Redis could not answer in time. Presence is then UNKNOWN (degraded), never 'offline'."""


# Shared Lua prelude: now in ms from Redis's clock, drop expired members, "does this user have a
# live connection" test. Member format is "<user_id>:<conn_id>"; user ids contain no colon.
_PRELUDE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local function has_live(user)
  local prefix = user .. ':'
  local live = redis.call('ZRANGE', KEYS[1], 0, -1)
  for _, m in ipairs(live) do
    if string.sub(m, 1, #prefix) == prefix then return true end
  end
  return false
end
"""

# KEYS: conns, avail. ARGV: member, user, ttl_ms, key_ttl_s.
_REGISTER = (
    _PRELUDE
    + """
local user = ARGV[2]
if not has_live(user) then redis.call('HDEL', KEYS[2], user) end  -- offline -> online resets it
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
if redis.call('EXISTS', KEYS[2]) == 1 then redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4])) end
return now
"""
)

# KEYS: conns, avail. ARGV: member, user.
_REMOVE = (
    _PRELUDE
    + """
redis.call('ZREM', KEYS[1], ARGV[1])
if not has_live(ARGV[2]) then redis.call('HDEL', KEYS[2], ARGV[2]) end
return now
"""
)

# KEYS: conns, avail. Returns a flat array: user, availability, user, availability, ...
_SNAPSHOT = (
    _PRELUDE
    + """
local seen, order = {}, {}
for _, m in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
  local user = string.match(m, '^([^:]+):')
  if user and not seen[user] then seen[user] = true; table.insert(order, user) end
end
local stored = redis.call('HGETALL', KEYS[2])
local avail = {}
for i = 1, #stored, 2 do
  if seen[stored[i]] then avail[stored[i]] = stored[i + 1]
  else redis.call('HDEL', KEYS[2], stored[i]) end  -- availability of an offline user is dropped
end
local out = {}
for _, user in ipairs(order) do
  table.insert(out, user)
  table.insert(out, avail[user] or 'available')
end
return out
"""
)

# KEYS: conns, avail. ARGV: user, value, key_ttl_s. Only for a user with a live connection.
_SET_AVAILABILITY = (
    _PRELUDE
    + """
if not has_live(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[3]))
return 1
"""
)

# KEYS: typing. ARGV: member, ttl_ms, key_ttl_s.
_TYPING_SET = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return now
"""

# KEYS: typing. ARGV: member.
_TYPING_CLEAR = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREM', KEYS[1], ARGV[1])
return now
"""

# KEYS: typing. Returns distinct user ids.
_TYPING_SNAPSHOT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local seen, out = {}, {}
for _, m in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
  local user = string.match(m, '^([^:]+):')
  if user and not seen[user] then seen[user] = true; table.insert(out, user) end
end
return out
"""


class PresenceStore:
    """All Redis operations for presence and typing. Every call is bounded by the Redis socket
    timeout and raises PresenceUnavailable on any Redis trouble (callers then report `degraded`)."""

    def __init__(self, settings: Settings, client: Redis | None = None) -> None:
        self._settings = settings
        self._prefix = settings.redis_channel_prefix
        self._client = client or create_redis_client(settings)
        self._timeout = settings.redis_socket_timeout_seconds
        self._expiry_ms = int(settings.presence_expiry_seconds * 1000)
        self._typing_ms = int(settings.typing_expiry_seconds * 1000)
        self._presence_key_ttl = int(settings.presence_expiry_seconds) + KEY_TTL_PADDING_SECONDS
        self._typing_key_ttl = int(settings.typing_expiry_seconds) + KEY_TTL_PADDING_SECONDS
        # register_script uses EVALSHA and transparently re-loads after a script-cache flush
        self._register = self._client.register_script(_REGISTER)
        self._remove = self._client.register_script(_REMOVE)
        self._snapshot = self._client.register_script(_SNAPSHOT)
        self._set_availability = self._client.register_script(_SET_AVAILABILITY)
        self._typing_set = self._client.register_script(_TYPING_SET)
        self._typing_clear = self._client.register_script(_TYPING_CLEAR)
        self._typing_snapshot = self._client.register_script(_TYPING_SNAPSHOT)

    # ---- key names --------------------------------------------------------------------------

    def conns_key(self, room_id: uuid.UUID) -> str:
        return f"{self._prefix}:presence:{room_id}:conns"

    def avail_key(self, room_id: uuid.UUID) -> str:
        return f"{self._prefix}:presence:{room_id}:avail"

    def typing_key(self, room_id: uuid.UUID) -> str:
        return f"{self._prefix}:typing:{room_id}"

    @staticmethod
    def member(user_id: uuid.UUID, conn_id: uuid.UUID) -> str:
        return f"{user_id}:{conn_id}"

    # ---- bounded execution ------------------------------------------------------------------

    async def _run(self, coro):  # noqa: ANN001, ANN202
        try:
            async with asyncio.timeout(self._timeout):
                return await coro
        except (TimeoutError, redis_exceptions.RedisError, OSError) as exc:
            raise PresenceUnavailable(type(exc).__name__) from None

    # ---- presence ---------------------------------------------------------------------------

    async def register(self, room_id: uuid.UUID, user_id: uuid.UUID, conn_id: uuid.UUID) -> None:
        """Add or refresh one connection (expires PRESENCE_EXPIRY seconds after Redis's clock)."""
        await self._run(
            self._register(
                keys=[self.conns_key(room_id), self.avail_key(room_id)],
                args=[
                    self.member(user_id, conn_id),
                    str(user_id),
                    self._expiry_ms,
                    self._presence_key_ttl,
                ],
            )
        )

    async def remove(self, room_id: uuid.UUID, user_id: uuid.UUID, conn_id: uuid.UUID) -> None:
        await self._run(
            self._remove(
                keys=[self.conns_key(room_id), self.avail_key(room_id)],
                args=[self.member(user_id, conn_id), str(user_id)],
            )
        )
        await self.typing_clear(room_id, user_id, conn_id)

    async def snapshot(self, room_id: uuid.UUID) -> list[tuple[uuid.UUID, Availability]]:
        flat = await self._run(
            self._snapshot(keys=[self.conns_key(room_id), self.avail_key(room_id)])
        )
        return [(uuid.UUID(flat[i]), flat[i + 1]) for i in range(0, len(flat), 2)]

    async def set_availability(
        self, room_id: uuid.UUID, user_id: uuid.UUID, value: Availability
    ) -> bool:
        """Only for a user with a live connection. False if they are offline."""
        done = await self._run(
            self._set_availability(
                keys=[self.conns_key(room_id), self.avail_key(room_id)],
                args=[str(user_id), value, self._presence_key_ttl],
            )
        )
        return bool(done)

    # ---- typing -----------------------------------------------------------------------------

    async def typing_start(
        self, room_id: uuid.UUID, user_id: uuid.UUID, conn_id: uuid.UUID
    ) -> None:
        await self._run(
            self._typing_set(
                keys=[self.typing_key(room_id)],
                args=[self.member(user_id, conn_id), self._typing_ms, self._typing_key_ttl],
            )
        )

    async def typing_clear(
        self, room_id: uuid.UUID, user_id: uuid.UUID, conn_id: uuid.UUID
    ) -> None:
        await self._run(
            self._typing_clear(
                keys=[self.typing_key(room_id)], args=[self.member(user_id, conn_id)]
            )
        )

    async def typing_snapshot(self, room_id: uuid.UUID) -> list[uuid.UUID]:
        users = await self._run(self._typing_snapshot(keys=[self.typing_key(room_id)]))
        return [uuid.UUID(u) for u in users]

    # ---- hints ------------------------------------------------------------------------------

    async def publish_hint(self, room_id: uuid.UUID) -> None:
        """Tell the other API processes to look again. Best effort: a lost hint delays an update
        by at most one presence tick."""
        with contextlib.suppress(PresenceUnavailable):
            await self._run(self._client.publish(presence_hint_channel(self._prefix, room_id), "1"))

    async def aclose(self) -> None:
        try:
            await asyncio.wait_for(self._client.aclose(), timeout=2)
        except Exception:
            log.debug("presence client close failed")
