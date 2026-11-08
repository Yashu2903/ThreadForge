"""Publishing to Redis/Valkey, with bounded timeouts and safe error codes.

PUBLISH is fire-and-forget Pub/Sub: Redis returns the number of subscribers that received it. Zero
subscribers is a perfectly good outcome (nobody is connected), so it counts as success. A
successful publish proves nothing about browser receipt.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import redis.asyncio as aioredis
from redis import exceptions as redis_exceptions

from app.config import Settings


class PublishError(Exception):
    """A publish failed. `code` is a short safe code (no URL, no credentials, no payload)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Publisher(Protocol):
    async def publish(self, channel: str, message: str) -> int: ...

    async def aclose(self) -> None: ...


def create_redis_client(settings: Settings) -> aioredis.Redis:
    """One Redis client. Connecting is lazy: building it never touches the network, so the API and
    the dispatcher start (and the API serves HTTP) while Redis is down."""
    return aioredis.Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=settings.redis_health_check_interval_seconds,
    )


class RedisPublisher:
    def __init__(self, settings: Settings) -> None:
        self._timeout = settings.outbox_publish_timeout_seconds
        self._client = create_redis_client(settings)

    async def publish(self, channel: str, message: str) -> int:
        try:
            async with asyncio.timeout(self._timeout):  # hard bound on connect + publish
                return await self._client.publish(channel, message)
        except (TimeoutError, redis_exceptions.TimeoutError):
            raise PublishError("redis_timeout") from None
        except redis_exceptions.AuthenticationError:  # a subclass of ConnectionError: test it first
            raise PublishError("redis_auth") from None
        except (redis_exceptions.ConnectionError, OSError):
            raise PublishError("redis_connection") from None
        except redis_exceptions.RedisError:
            raise PublishError("redis_error") from None

    async def aclose(self) -> None:
        await self._client.aclose()
