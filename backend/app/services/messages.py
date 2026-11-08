"""Messages: the ONLY module that mutates the messages and message_write_buckets tables.

One function per mutation (send_message, edit_message, delete_message), each with exactly one
commit point (the end of its `async with sessionmaker() as db, db.begin()` block), so Part 4 can
add its room-event and outbox inserts and the per-room event-sequence increment inside the same
transaction, under the room lock that is already held.

TRANSACTION PATTERN (Part 2's, unchanged): a service takes the sessionmaker, never an existing
session, and opens its own session + transaction. READ COMMITTED is the isolation level and must
stay that way: every in-lock lookup relies on each statement seeing the latest committed data.

LOCK ORDER for every write:
    1. room row         SELECT ... FOR NO KEY UPDATE   (lock_room_and_actor, shared with Part 2)
    2. message row      SELECT ... FOR UPDATE          (edit / delete)
    3. rate-limit row   INSERT ... ON CONFLICT DO UPDATE on message_write_buckets
Every membership mutation also takes the room lock first, so holding it guarantees the caller's
membership and role cannot change underneath the request; no FOR SHARE on the membership row is
needed. TRADE-OFF: all message writes in one room are serialized. At ThreadForge room sizes and
write limits that is cheap, and Part 4's per-room event sequence needs this same lock anyway.
Reads (GETs) take no locks.

Responses are built only AFTER the commit, from a fresh read. No network calls in transactions.
Message bodies are never logged; log lines carry ids only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, extract, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import db_error_details
from app.errors import (
    insufficient_role,
    message_id_reused,
    message_not_found,
    rate_limited,
)
from app.logging_config import APP_LOGGER, request_id_var
from app.models import Message, MessageWriteBucket, User
from app.services.rooms import lock_room_and_actor

log = logging.getLogger(f"{APP_LOGGER}.messages")

_MESSAGES = Message.__table__
_BUCKETS = MessageWriteBucket.__table__
_USERS = User.__table__

WRITER_ROLES = ("owner", "editor")
UNIQUE_VIOLATION = "23505"
CLIENT_ID_CONSTRAINT = "uq_messages_room_author_client"
FINGERPRINT_VERSION = b"msg-create-v1"
DELETED_USER_NAME = "Deleted user"


@dataclass(frozen=True)
class MessageView:
    id: uuid.UUID
    room_id: uuid.UUID
    author_id: uuid.UUID
    author_display_name: str
    client_message_id: uuid.UUID
    body: str | None
    created_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None
    deleted_by: uuid.UUID | None
    version: int
    is_deleted: bool


@dataclass(frozen=True)
class SendResult:
    message: MessageView
    created: bool  # True: a new row (201). False: a matching retry of an earlier send (200).


def log_message_event(
    action: str, room_id: uuid.UUID, message_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """Structured line: ids only, never content."""
    log.info(
        "action=%s room_id=%s message_id=%s actor_id=%s request_id=%s",
        action,
        room_id,
        message_id,
        actor_id,
        request_id_var.get(),
    )


# ---- request fingerprint ------------------------------------------------------------------------


def compute_fingerprint(settings: Settings, client_message_id: uuid.UUID, body: str) -> bytes:
    """HMAC-SHA256 over a deterministic, versioned serialization of the creation fields.

    Why keyed: a plain unsalted hash of a short message can be reversed by guessing candidate
    texts, which would defeat deletion (the row would keep an offline-checkable copy of the text).
    Without the secret an HMAC cannot be tested that way. Retaining a keyed fingerprint is not the
    same as retaining the text. Rotating MESSAGE_FINGERPRINT_SECRET makes in-flight retries fail
    with message_id_reused, which is acceptable because retries happen within seconds.
    Deduplication is by client_message_id ONLY, never by matching text.
    """
    canonical = json.dumps(
        {"client_message_id": str(client_message_id), "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    key = settings.message_fingerprint_secret.get_secret_value().encode("utf-8")
    return hmac.new(key, FINGERPRINT_VERSION + b"\n" + canonical, hashlib.sha256).digest()


# ---- shared building blocks ----------------------------------------------------------------------


def _message_columns():
    """Columns of the one message representation. A deleted or missing author shows as
    'Deleted user'; no per-message queries (always one JOIN)."""
    name = case(
        (_USERS.c.id.is_(None), DELETED_USER_NAME),
        (_USERS.c.state == "deleted", DELETED_USER_NAME),
        else_=_USERS.c.display_name,
    ).label("author_display_name")
    return [
        _MESSAGES.c.id,
        _MESSAGES.c.room_id,
        _MESSAGES.c.author_id,
        name,
        _MESSAGES.c.client_message_id,
        _MESSAGES.c.body,
        _MESSAGES.c.created_at,
        _MESSAGES.c.edited_at,
        _MESSAGES.c.deleted_at,
        _MESSAGES.c.deleted_by,
        _MESSAGES.c.version,
    ]


def row_to_view(row) -> MessageView:
    return MessageView(**row._mapping, is_deleted=row.deleted_at is not None)


def _joined_select():
    return select(*_message_columns()).select_from(
        _MESSAGES.outerjoin(_USERS, _USERS.c.id == _MESSAGES.c.author_id)
    )


async def get_message(
    sessionmaker: async_sessionmaker[AsyncSession], room_id: uuid.UUID, message_id: uuid.UUID
) -> MessageView | None:
    """One message (or tombstone), scoped by BOTH room and id. No locks."""
    async with sessionmaker() as db:
        row = (
            await db.execute(
                _joined_select().where(_MESSAGES.c.id == message_id, _MESSAGES.c.room_id == room_id)
            )
        ).one_or_none()
    return row_to_view(row) if row else None


async def _authorize_writer(db: AsyncSession, room_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    """Step 1 of every write: lock the room, THEN recheck membership and role in-transaction.
    Nonmember/removed -> room_not_found (404); viewer -> insufficient_role (403)."""
    _room, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
    if role not in WRITER_ROLES:
        raise insufficient_role()
    return role


async def _consume_write_capacity(
    db: AsyncSession, settings: Settings, room_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Fixed-window rate limit: ONE atomic statement in the caller's transaction.

        INSERT INTO message_write_buckets (room_id, user_id, window_start, count)
        VALUES (:room, :user, date_trunc('minute', clock_timestamp()), 1)
        ON CONFLICT (room_id, user_id, window_start)
        DO UPDATE SET count = message_write_buckets.count + 1
        WHERE message_write_buckets.count < :limit
        RETURNING count

    No row returned means the window is full: raise rate_limited (the caller's transaction rolls
    back, so nothing changed). The window and Retry-After come from the DATABASE clock. Because the
    increment shares the message change's transaction, a failed or rolled-back write consumes no
    capacity. FIXED window: near a minute boundary a user can burst up to twice the limit.
    """
    statement = (
        pg_insert(_BUCKETS)
        .values(
            room_id=room_id,
            user_id=user_id,
            window_start=func.date_trunc("minute", func.clock_timestamp()),
            count=1,
        )
        .on_conflict_do_update(
            index_elements=[_BUCKETS.c.room_id, _BUCKETS.c.user_id, _BUCKETS.c.window_start],
            set_={"count": _BUCKETS.c.count + 1},
            where=_BUCKETS.c.count < settings.message_writes_per_minute,
        )
        .returning(_BUCKETS.c.count)
    )
    if (await db.execute(statement)).first() is None:
        seconds_left = (
            await db.execute(
                select(
                    extract(
                        "epoch",
                        func.date_trunc("minute", func.clock_timestamp())
                        + text("interval '1 minute'")
                        - func.clock_timestamp(),
                    )
                )
            )
        ).scalar_one()
        raise rate_limited(max(1, math.ceil(seconds_left)))


# ---- send ------------------------------------------------------------------------------------


async def send_message(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    client_message_id: uuid.UUID,
    body: str,
) -> SendResult:
    """Create a message, or answer a retry of an earlier send. Single commit point per attempt.

    In the transaction, after the room lock: (1) recheck membership and write role; (2) look up
    (room, author, client_message_id); (3) found: matching fingerprint -> return it unchanged (no
    write, no rate-limit use), different fingerprint -> 409 message_id_reused; (4) not found:
    consume rate-limit capacity; (5) INSERT with version 1, the fingerprint and clock_timestamp();
    (6) commit, then re-read for the response.

    If the INSERT raises a unique violation on uq_messages_room_author_client (it cannot while the
    room lock serializes writers; the constraint is the final safeguard), the transaction is rolled
    back and the whole thing re-runs once in a fresh transaction, which then answers as a retry.
    Permission is checked BEFORE the existing row is returned, so a removed or downgraded user
    cannot use a retry to bypass current restrictions.
    """
    fingerprint = compute_fingerprint(settings, client_message_id, body)
    message_id: uuid.UUID
    created: bool
    for attempt in (1, 2):
        try:
            async with sessionmaker() as db, db.begin():
                await _authorize_writer(db, room_id, actor_id)
                existing = (
                    await db.execute(
                        select(_MESSAGES.c.id, _MESSAGES.c.initial_request_fingerprint).where(
                            _MESSAGES.c.room_id == room_id,
                            _MESSAGES.c.author_id == actor_id,
                            _MESSAGES.c.client_message_id == client_message_id,
                        )
                    )
                ).one_or_none()
                if existing is not None:
                    if not hmac.compare_digest(
                        bytes(existing.initial_request_fingerprint), fingerprint
                    ):
                        raise message_id_reused()
                    message_id, created = existing.id, False
                else:
                    await _consume_write_capacity(db, settings, room_id, actor_id)
                    message_id = (
                        await db.execute(
                            insert(_MESSAGES)
                            .values(
                                room_id=room_id,
                                author_id=actor_id,
                                client_message_id=client_message_id,
                                body=body,
                                initial_request_fingerprint=fingerprint,
                                # SQL clock_timestamp(), read AFTER the room lock is held (never
                                # now(): that is the transaction's START time).
                                created_at=func.clock_timestamp(),
                            )
                            .returning(_MESSAGES.c.id)
                        )
                    ).scalar_one()
                    created = True
            break  # committed
        except IntegrityError as exc:
            if attempt == 2 or db_error_details(exc) != (UNIQUE_VIOLATION, CLIENT_ID_CONSTRAINT):
                raise
            log.info("duplicate send raced past the lookup; retrying as a retry")

    view = await get_message(sessionmaker, room_id, message_id)
    if view is None:  # cannot happen: rows are never physically deleted
        raise message_not_found()
    if created:
        log_message_event("message_sent", room_id, message_id, actor_id)
    return SendResult(view, created)
