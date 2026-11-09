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
membership cannot change underneath the request; no FOR SHARE on the membership row is
needed. TRADE-OFF: all message writes in one room are serialized. At ThreadForge room sizes and
write limits that is cheap, and Part 4's per-room event sequence needs this same lock anyway.
Reads (GETs) take no locks.

Responses are built only AFTER the commit, from a fresh read. No network calls in transactions.
Message bodies are never logged; log lines carry ids only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, extract, func, insert, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import db_error_details
from app.errors import (
    invalid_cursor,
    message_deleted,
    message_id_reused,
    message_locked,
    message_not_author,
    message_not_found,
    message_version_conflict,
    rate_limited,
)
from app.logging_config import APP_LOGGER, request_id_var
from app.models import Message, MessageWriteBucket, User
from app.services.room_events import record_room_event
from app.services.rooms import lock_room_and_actor

log = logging.getLogger(f"{APP_LOGGER}.messages")

_MESSAGES = Message.__table__
_BUCKETS = MessageWriteBucket.__table__
_USERS = User.__table__

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
    kind: str = "user"  # user | ai_request | ai_answer
    ai_request_id: uuid.UUID | None = None


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
        _MESSAGES.c.kind,
        _MESSAGES.c.ai_request_id,
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


async def _authorize_member(db: AsyncSession, room_id: uuid.UUID, actor_id: uuid.UUID):
    """Step 1 of every write: lock the room, THEN recheck ACTIVE MEMBERSHIP in-transaction.

    Chat depends only on being an active member: role never gates sending, editing or deleting
    your own message (role restrictions belong to documents and uploads in later parts). A
    nonmember or removed user gets room_not_found (404). The role is returned only so that
    delete_message can apply the one role rule left in chat: the owner may delete anyone's message.
    Returns (LockedRoom, role): the handle record_room_event() needs to record the room event.
    """
    locked, role = await lock_room_and_actor(db, room_id, actor_id, owner_only=False)
    return locked, role


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


# ---- shared insert (also used by the AI request and reply paths) -----------------------------------


async def insert_message_row(
    db: AsyncSession,
    locked,
    *,
    room_id: uuid.UUID,
    author_id: uuid.UUID,
    client_message_id: uuid.UUID,
    body: str,
    fingerprint: bytes,
    kind: str = "user",
    ai_request_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """INSERT one message and record its message_created event, inside the caller's transaction (the
    room lock must be held; `locked` proves it). `kind` and `ai_request_id` are immutable afterwards
    (a database trigger enforces it). `actor_id` is who the event is attributed to (the author, except
    for AI replies, which are attributed to the person who asked)."""
    inserted = (
        await db.execute(
            insert(_MESSAGES)
            .values(
                room_id=room_id,
                author_id=author_id,
                client_message_id=client_message_id,
                body=body,
                initial_request_fingerprint=fingerprint,
                created_at=func.clock_timestamp(),
                kind=kind,
                ai_request_id=ai_request_id,
            )
            .returning(_MESSAGES.c.id, _MESSAGES.c.version)
        )
    ).one()
    await record_room_event(
        db,
        locked,
        event_type="message_created",
        resource_type="message",
        resource_id=inserted.id,
        resource_version=inserted.version,
        actor_id=actor_id or author_id,
        payload={},
    )
    return inserted.id


async def consume_write_capacity(
    db: AsyncSession, settings: Settings, room_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """The human write bucket (an @AI message is a message: it counts)."""
    await _consume_write_capacity(db, settings, room_id, user_id)


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

    In the transaction, after the room lock: (1) recheck active membership (any role); (2) look up
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
                locked, _role = await _authorize_member(db, room_id, actor_id)
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
                    message_id = await insert_message_row(
                        db,
                        locked,
                        room_id=room_id,
                        author_id=actor_id,
                        client_message_id=client_message_id,
                        body=body,
                        fingerprint=fingerprint,
                    )
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


# ---- history (reads take no locks) ------------------------------------------------------

HISTORY_CURSOR_VERSION = "m1"
_CURSOR_ALPHABET = re.compile(r"[A-Za-z0-9_-]{1,300}")
_CURSOR_PAYLOAD = re.compile(
    r"(m\d+)\|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\|"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00)\|"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def encode_history_cursor(room_id: uuid.UUID, created_at: datetime, message_id: uuid.UUID) -> str:
    """Opaque URL-safe base64 of a versioned payload: version | room | created_at | id.

    created_at keeps exact microseconds (PostgreSQL's timestamptz precision), so rows sharing a
    timestamp are neither skipped nor repeated (the id breaks ties). The cursor is UNSIGNED: it is a
    pagination position, never an authorization credential. Forging one can only move the position
    within a room the caller can already read; every request is still authorized on its own.
    """
    stamp = created_at.astimezone(UTC).isoformat(timespec="microseconds")
    payload = f"{HISTORY_CURSOR_VERSION}|{room_id}|{stamp}|{message_id}"
    return base64.urlsafe_b64encode(payload.encode("ascii")).rstrip(b"=").decode("ascii")


def decode_history_cursor(raw: str, room_id: uuid.UUID) -> tuple[datetime, uuid.UUID]:
    """Strict parse. Any malformed, non-canonical, wrong-version or cross-room cursor is
    invalid_cursor (422)."""
    try:
        if not _CURSOR_ALPHABET.fullmatch(raw):
            raise ValueError
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != raw:
            raise ValueError  # non-canonical encoding
        match = _CURSOR_PAYLOAD.fullmatch(decoded.decode("ascii"))
        if match is None or match.group(1) != HISTORY_CURSOR_VERSION:
            raise ValueError
        if uuid.UUID(match.group(2)) != room_id:
            raise ValueError  # a cursor from another room
        return datetime.fromisoformat(match.group(3)), uuid.UUID(match.group(4))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise invalid_cursor() from None


async def list_messages(
    sessionmaker: async_sessionmaker[AsyncSession],
    room_id: uuid.UUID,
    limit: int,
    before: str | None,
) -> tuple[list[MessageView], str | None]:
    """One page of history: the newest `limit` messages (or the newest older than `before`),
    returned oldest-to-newest, plus a cursor for the next OLDER page (or None).

        SELECT ... FROM messages LEFT JOIN users ... WHERE room_id = :room
          [AND (created_at, id) < (:ts, :id)]
        ORDER BY created_at DESC, id DESC LIMIT :limit + 1

    Uses the (room_id, created_at, id) index; no OFFSET. limit + 1 rows reveal whether another
    older page exists without a count query. Tombstones are included. ONE query joins users, so
    the statement count does not grow with the number of messages. This is history pagination,
    not a synchronization protocol, and not a frozen snapshot: messages written between two page
    requests can appear on a later "newest" fetch, and edits or deletions show up on re-fetch.
    """
    async with sessionmaker() as db:
        return await fetch_message_page(db, room_id, limit, before)


async def fetch_message_page(
    db: AsyncSession, room_id: uuid.UUID, limit: int, before: str | None
) -> tuple[list[MessageView], str | None]:
    """The query behind list_messages, on a session the CALLER owns. GET /rooms/{id}/sync runs it
    inside its REPEATABLE READ snapshot so the page and through_seq come from one point in time."""
    after = decode_history_cursor(before, room_id) if before is not None else None
    query = (
        _joined_select()
        .where(_MESSAGES.c.room_id == room_id)
        .order_by(_MESSAGES.c.created_at.desc(), _MESSAGES.c.id.desc())
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(tuple_(_MESSAGES.c.created_at, _MESSAGES.c.id) < tuple_(*after))
    fetched = (await db.execute(query)).all()
    page, extra = fetched[:limit], fetched[limit:]
    next_before = (
        encode_history_cursor(room_id, page[-1].created_at, page[-1].id) if extra else None
    )  # page[-1] is the OLDEST row returned (the page is in descending order here)
    return [row_to_view(row) for row in reversed(page)], next_before


# ---- edit and delete ---------------------------------------------------------------------------


async def _lock_message(db: AsyncSession, room_id: uuid.UUID, message_id: uuid.UUID):
    """Lock-order step 2: the message row FOR UPDATE, scoped by BOTH room and id (so another
    room's message id is simply not found)."""
    return (
        await db.execute(
            select(
                _MESSAGES.c.id,
                _MESSAGES.c.author_id,
                _MESSAGES.c.body,
                _MESSAGES.c.version,
                _MESSAGES.c.deleted_at,
                _MESSAGES.c.kind,
                _MESSAGES.c.ai_request_id,
            )
            .where(_MESSAGES.c.id == message_id, _MESSAGES.c.room_id == room_id)
            .with_for_update()
        )
    ).one_or_none()


async def edit_message(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    message_id: uuid.UUID | None,
    body: str,
    expected_version: int,
) -> MessageView:
    """Edit with conflict protection. Single commit point. After the room lock, in this order:

    1. recheck active membership (404 room_not_found; the author must STILL be an active member,
       of any role: a viewer may edit their own messages);
    2. load the message, scoped to the room, FOR UPDATE            -> 404 message_not_found;
    3. caller is not the author (the owner cannot edit others')     -> 403 message_not_author;
    4. already deleted (a tombstone is final)                       -> 409 message_deleted;
    5. expected_version != current version                          -> 409 message_version_conflict
       (with current_version); a stale version conflicts even when the text happens to match;
    6. submitted body == current body: return it, change nothing (no version bump, no timestamp,
       no rate-limit use);
    7. consume rate-limit capacity                                  -> 429 rate_limited;
    8. UPDATE body, edited_at = clock_timestamp(), version = version + 1.

    There is no separate edit idempotency key in this part: a retry of an edit whose first attempt
    landed carries the old expected_version and so gets a conflict, which tells the client to
    reload. Only the latest text is kept: there is no message_versions table.
    """
    if message_id is None:
        raise message_not_found()
    changed = False
    async with sessionmaker() as db, db.begin():
        locked, _role = await _authorize_member(db, room_id, actor_id)
        message = await _lock_message(db, room_id, message_id)
        if message is None:
            raise message_not_found()
        if message.author_id != actor_id:
            raise message_not_author()
        if message.deleted_at is not None:
            raise message_deleted()
        if message.kind != "user":
            raise message_locked()  # AI requests and answers are never edited, and an edit never reruns AI
        if message.version != expected_version:
            raise message_version_conflict(message.version)
        if body != message.body:
            await _consume_write_capacity(db, settings, room_id, actor_id)
            new_version = (
                await db.execute(
                    update(_MESSAGES)
                    .where(_MESSAGES.c.id == message_id)
                    .values(
                        body=body,
                        edited_at=func.clock_timestamp(),
                        version=_MESSAGES.c.version + 1,
                    )
                    .returning(_MESSAGES.c.version)
                )
            ).scalar_one()
            await record_room_event(
                db,
                locked,
                event_type="message_edited",
                resource_type="message",
                resource_id=message_id,
                resource_version=new_version,
                actor_id=actor_id,
                payload={},
            )
            changed = True
    view = await get_message(sessionmaker, room_id, message_id)
    if view is None:  # cannot happen
        raise message_not_found()
    if changed:
        log_message_event("message_edited", room_id, message_id, actor_id)
    return view


async def delete_message(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor_id: uuid.UUID,
    room_id: uuid.UUID,
    message_id: uuid.UUID | None,
    expected_version: int,
) -> None:
    """Soft delete: the row stays as a tombstone (body NULL); text is never recoverable. Single
    commit point. After the room lock, in this order:

    1. recheck active membership (any role; 404 room_not_found otherwise);
    2. load the message, scoped to the room, FOR UPDATE            -> 404 message_not_found;
    3. the owner may delete anyone's, every other member only their own -> 403 message_not_author;
    4. already deleted: success (204), no change, no rate-limit use, REGARDLESS of expected_version;
    5. expected_version != current version                         -> 409 message_version_conflict;
    6. consume rate-limit capacity                                 -> 429 rate_limited;
    7. body = NULL, deleted_at = clock_timestamp(), deleted_by = actor, version = version + 1.

    Authorization (3) comes before the already-deleted shortcut (4) so that a caller who may not
    touch the message learns nothing about its state.
    """
    if message_id is None:
        raise message_not_found()
    changed = False
    async with sessionmaker() as db, db.begin():
        locked, role = await _authorize_member(db, room_id, actor_id)
        message = await _lock_message(db, room_id, message_id)
        if message is None:
            raise message_not_found()
        if role != "owner" and message.author_id != actor_id:
            raise message_not_author()
        if message.deleted_at is None:
            if message.version != expected_version:
                raise message_version_conflict(message.version)
            await _consume_write_capacity(db, settings, room_id, actor_id)
            new_version = (
                await db.execute(
                    update(_MESSAGES)
                    .where(_MESSAGES.c.id == message_id)
                    .values(
                        body=None,
                        deleted_at=func.clock_timestamp(),
                        deleted_by=actor_id,
                        version=_MESSAGES.c.version + 1,
                    )
                    .returning(_MESSAGES.c.version)
                )
            ).scalar_one()
            if message.kind != "user" and message.ai_request_id is not None:
                from app.services import ai_requests  # late import: that module imports this one

                await ai_requests.on_message_deleted(
                    db, locked, message.kind, message.ai_request_id, actor_id
                )
            await record_room_event(
                db,
                locked,
                event_type="message_deleted",
                resource_type="message",
                resource_id=message_id,
                resource_version=new_version,
                actor_id=actor_id,
                payload={},
            )
            changed = True
    if changed:
        log_message_event("message_deleted", room_id, message_id, actor_id)


async def chat_chars(db: AsyncSession, room_id: uuid.UUID, ids: list[uuid.UUID]) -> int:
    """Total characters of the given LIVE messages of this room (deleted ones and ids from other
    rooms count as zero). Read-only; used to estimate how much chat context a request would send."""
    if not ids:
        return 0
    total = (
        await db.execute(
            select(func.coalesce(func.sum(func.length(_MESSAGES.c.body)), 0)).where(
                _MESSAGES.c.room_id == room_id,
                _MESSAGES.c.id.in_(ids),
                _MESSAGES.c.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    return int(total)


async def live_ids(
    db: AsyncSession, room_id: uuid.UUID, ids: list[uuid.UUID], *, exclude_kind: str | None = None
) -> list[uuid.UUID]:
    """Which of these ids are LIVE (not deleted) messages of THIS room, optionally not of one kind.
    Read-only; the AI request path uses it to validate explicitly chosen chat context."""
    if not ids:
        return []
    query = select(_MESSAGES.c.id).where(
        _MESSAGES.c.room_id == room_id,
        _MESSAGES.c.id.in_(ids),
        _MESSAGES.c.deleted_at.is_(None),
    )
    if exclude_kind is not None:
        query = query.where(_MESSAGES.c.kind != exclude_kind)
    return list((await db.execute(query)).scalars().all())


async def is_deleted(db: AsyncSession, message_id: uuid.UUID) -> bool:
    return (
        await db.execute(select(_MESSAGES.c.deleted_at).where(_MESSAGES.c.id == message_id))
    ).scalar_one() is not None


@dataclass(frozen=True)
class ChatCopy:
    id: uuid.UUID
    body: str | None
    created_at: datetime
    author_name: str | None
    deleted: bool
    kind: str


async def chat_copies(db: AsyncSession, room_id: uuid.UUID, ids: list[str]) -> list[ChatCopy]:
    """Exact copies of the named messages of THIS room, oldest first (AI answers excluded: earlier AI
    output is never context). Deleted ones come back flagged, with no body."""
    if not ids:
        return []
    uuids = [uuid.UUID(i) for i in ids]
    rows = (
        await db.execute(
            select(
                _MESSAGES.c.id,
                _MESSAGES.c.body,
                _MESSAGES.c.created_at,
                _MESSAGES.c.deleted_at,
                _MESSAGES.c.kind,
                _USERS.c.display_name,
            )
            .select_from(_MESSAGES.outerjoin(_USERS, _USERS.c.id == _MESSAGES.c.author_id))
            .where(
                _MESSAGES.c.room_id == room_id,
                _MESSAGES.c.id.in_(uuids),
                _MESSAGES.c.kind != "ai_answer",
            )
            .order_by(_MESSAGES.c.created_at, _MESSAGES.c.id)
        )
    ).all()
    return [
        ChatCopy(r.id, r.body, r.created_at, r.display_name, r.deleted_at is not None, r.kind)
        for r in rows
    ]
