"""The PostgreSQL fixed-window write limit (per user, per room, per minute).

Every test here starts only when at least SAFE_SECONDS remain in the current database minute
(waiting if necessary), so a window rollover mid-test cannot make it flaky. Time-dependent
behaviour (the previous window, old buckets) is tested by inserting rows with chosen timestamps.
"""

import asyncio
import uuid
from contextlib import AsyncExitStack

import pytest
import pytest_asyncio

from app.commands.cleanup_auth import cleanup_auth
from app.services import messages as messages_service
from app.services.sessions import create_session
from tests.factories import make_settings
from tests.helpers import (
    Actor,
    add_member,
    execute,
    insert_messages,
    make_client,
    put_cookie,
    rows,
    scalar,
)
from tests.test_invitations_api import new_room
from tests.test_messages_edit_delete import delete, edit, row
from tests.test_messages_send import send
from tests.test_rooms_concurrency import contended

SAFE_SECONDS = 8
LIMIT = 5


@pytest_asyncio.fixture(autouse=True)
async def _start_early_in_the_minute(sessionmaker):
    while True:
        left = float(
            await scalar(
                sessionmaker,
                "SELECT extract(epoch FROM date_trunc('minute', clock_timestamp())"
                " + interval '1 minute' - clock_timestamp())",
            )
        )
        if left >= SAFE_SECONDS:
            return
        await asyncio.sleep(left + 0.3)  # wait for the next window; this is not a time assertion


@pytest.fixture
def limited():
    return make_settings(message_writes_per_minute=LIMIT)


async def bucket_total(sm, room_id=None):
    if room_id is None:
        return await scalar(sm, "SELECT coalesce(sum(count), 0) FROM message_write_buckets")
    return await scalar(
        sm,
        "SELECT coalesce(sum(count), 0) FROM message_write_buckets WHERE room_id = :r",
        r=room_id,
    )


async def room_for(make_actor, limited, sm, role="editor"):
    owner = await make_actor("Owner", app_settings=limited)
    room_id = await new_room(owner)
    user = await make_actor("Writer", app_settings=limited)
    await add_member(sm, room_id, user.id, role)
    return owner, user, room_id


async def test_exactly_the_limit_succeeds_under_concurrency_and_the_rest_get_429(
    make_actor, limited, sessionmaker
):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    results = await contended(
        sessionmaker, room_id, *[send(writer, room_id, f"burst {i}") for i in range(LIMIT + 10)]
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201] * LIMIT + [429] * 10
    for response in (r for r in results if r.status_code == 429):
        assert response.json()["error"]["code"] == "rate_limited"
        retry_after = response.headers["retry-after"]
        assert retry_after.isdigit() and 1 <= int(retry_after) <= 60
        assert response.headers["cache-control"] == "no-store"
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == LIMIT
    assert await bucket_total(sessionmaker, room_id) == LIMIT


async def test_retry_after_is_the_seconds_until_the_next_database_minute(
    make_actor, limited, sessionmaker
):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    for i in range(LIMIT):
        assert (await send(writer, room_id, f"m{i}")).status_code == 201
    blocked = await send(writer, room_id, "one too many")
    left = float(
        await scalar(
            sessionmaker,
            "SELECT extract(epoch FROM date_trunc('minute', clock_timestamp())"
            " + interval '1 minute' - clock_timestamp())",
        )
    )
    assert blocked.status_code == 429
    assert abs(int(blocked.headers["retry-after"]) - left) <= 2  # the same clock, a moment apart


async def test_sends_edits_and_deletes_share_one_bucket(make_actor, limited, sessionmaker):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    m1, m2, m3, m4 = await insert_messages(sessionmaker, room_id, writer.id, 4)  # not rate-limited
    assert (await edit(writer, room_id, m1, "edit 1", 1)).status_code == 200  # 1
    assert (await delete(writer, room_id, m2, 1)).status_code == 204  # 2
    assert (await send(writer, room_id, "new a")).status_code == 201  # 3
    assert (await send(writer, room_id, "new b")).status_code == 201  # 4
    assert (await edit(writer, room_id, m3, "edit 3", 1)).status_code == 200  # 5
    assert await bucket_total(sessionmaker, room_id) == LIMIT
    # the window is full: each kind of write is refused
    for response in (
        await send(writer, room_id, "new c"),
        await edit(writer, room_id, m1, "edit again", 2),
        await delete(writer, room_id, m4, 1),
    ):
        assert (response.status_code, response.json()["error"]["code"]) == (429, "rate_limited")
    assert await bucket_total(sessionmaker, room_id) == LIMIT  # refused writes consumed nothing


async def test_a_rejected_write_leaves_the_message_unchanged(make_actor, limited, sessionmaker):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    m1, m2 = await insert_messages(sessionmaker, room_id, writer.id, 2)
    for i in range(LIMIT):
        await send(writer, room_id, f"fill {i}")
    before = (await row(sessionmaker, m1), await row(sessionmaker, m2))
    assert (await edit(writer, room_id, m1, "blocked edit", 1)).status_code == 429
    assert (await delete(writer, room_id, m2, 1)).status_code == 429
    count = await scalar(sessionmaker, "SELECT count(*) FROM messages")
    assert (await send(writer, room_id, "blocked send")).status_code == 429
    assert (await row(sessionmaker, m1), await row(sessionmaker, m2)) == before
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == count


async def test_matching_retries_unchanged_edits_and_repeated_deletes_use_no_capacity(
    make_actor, limited, sessionmaker
):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    cmid = str(uuid.uuid4())
    sent = (await send(writer, room_id, "once", cmid)).json()  # 1
    (existing,) = await insert_messages(sessionmaker, room_id, writer.id, 1, prefix="keep")
    await delete(writer, room_id, existing, 1)  # 2
    assert await bucket_total(sessionmaker, room_id) == 2
    for _ in range(5):
        assert (await send(writer, room_id, "once", cmid)).status_code == 200  # matching retries
        assert (await edit(writer, room_id, sent["id"], "once", 1)).status_code == 200  # unchanged
        assert (await delete(writer, room_id, existing, 1)).status_code == 204  # repeated delete
    assert await bucket_total(sessionmaker, room_id) == 2  # still just the two real writes
    for i in range(LIMIT - 2):  # and three more real writes still fit
        assert (await send(writer, room_id, f"real {i}")).status_code == 201
    assert (await send(writer, room_id, "now full")).status_code == 429


async def test_failed_requests_before_the_write_use_no_capacity(make_actor, limited, sessionmaker):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    cmid = str(uuid.uuid4())
    await send(writer, room_id, "original", cmid)  # 1
    other_room = await new_room(owner, "other")
    sent = (await send(writer, room_id, "second")).json()  # 2
    attempts = [
        await send(writer, room_id, "different", cmid),  # 409 message_id_reused
        await edit(writer, room_id, sent["id"], "x", 99),  # 409 version conflict
        await edit(owner, room_id, sent["id"], "x", 1),  # 403 not author
        await delete(owner, other_room, sent["id"], 1),  # 404 wrong room
        await writer.post(f"/rooms/{room_id}/messages", json={"body": "no id"}),  # 422
        await send(writer, other_room, "not a member"),  # 404
    ]
    assert [r.status_code for r in attempts] == [409, 409, 403, 404, 422, 404]
    assert await bucket_total(sessionmaker) == 2


async def test_a_bucket_for_the_previous_minute_does_not_block_a_new_write(
    make_actor, limited, sessionmaker
):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    await execute(
        sessionmaker,
        "INSERT INTO message_write_buckets (room_id, user_id, window_start, count) "
        "VALUES (:r, :u, date_trunc('minute', clock_timestamp()) - interval '1 minute', :c)",
        r=uuid.UUID(room_id),
        u=writer.id,
        c=LIMIT,  # last minute's window was completely full
    )
    assert (await send(writer, room_id, "fresh window")).status_code == 201
    counts = await rows(
        sessionmaker, "SELECT count FROM message_write_buckets ORDER BY window_start"
    )
    assert [c["count"] for c in counts] == [LIMIT, 1]


async def test_a_rolled_back_transaction_does_not_consume_capacity(
    make_actor, limited, sessionmaker
):
    """The increment shares the message change's transaction. A failure AFTER the capacity was
    consumed (here: the body CHECK constraint fires on the INSERT/UPDATE) rolls it back."""
    from sqlalchemy.exc import IntegrityError

    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    (target,) = await insert_messages(sessionmaker, room_id, writer.id, 1)
    rid = uuid.UUID(room_id)
    with pytest.raises(IntegrityError):
        await messages_service.send_message(
            sessionmaker, limited, writer.id, rid, uuid.uuid4(), "x" * 5001
        )
    with pytest.raises(IntegrityError):
        await messages_service.edit_message(
            sessionmaker, limited, writer.id, rid, target, "x" * 5001, 1
        )
    assert await bucket_total(sessionmaker) == 0  # nothing consumed
    assert (await row(sessionmaker, target))["version"] == 1
    for i in range(LIMIT):  # the full limit is still available
        assert (await send(writer, room_id, f"ok {i}")).status_code == 201


async def test_the_bucket_is_shared_across_separate_sessions_of_the_same_user(
    make_actor, limited, sessionmaker, engine, google
):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    async with AsyncExitStack() as stack:
        async with sessionmaker() as db, db.begin():  # a second login (cookie) for the same user
            second = await create_session(db, limited, writer.id)
        client = await stack.enter_async_context(make_client(limited, engine, google))
        put_cookie(client, limited.session_cookie_name, second.secret)
        other_browser = Actor(client, writer.id, "Writer")
        results = await contended(
            sessionmaker,
            room_id,
            *[send(writer if i % 2 else other_browser, room_id, f"m{i}") for i in range(8)],
        )
    assert sorted(r.status_code for r in results) == [201] * LIMIT + [429] * 3
    assert await bucket_total(sessionmaker, room_id) == LIMIT


async def test_buckets_are_per_user_and_per_room(make_actor, limited, sessionmaker):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    other_room = await new_room(owner, "other")
    await add_member(sessionmaker, other_room, writer.id, "editor")
    for i in range(LIMIT):
        assert (await send(writer, room_id, f"m{i}")).status_code == 201
    assert (await send(writer, room_id, "full")).status_code == 429
    assert (await send(writer, other_room, "other room: own bucket")).status_code == 201
    assert (await send(owner, room_id, "other user: own bucket")).status_code == 201


async def test_the_limit_comes_from_the_setting(make_actor, sessionmaker):
    tiny = make_settings(message_writes_per_minute=1)
    owner = await make_actor("Owner", app_settings=tiny)
    room_id = await new_room(owner)
    assert (await send(owner, room_id, "one")).status_code == 201
    assert (await send(owner, room_id, "two")).status_code == 429


# ---- cleanup of old buckets -----------------------------------------------------------------------------


async def test_cleanup_deletes_only_old_buckets_in_batches_and_never_messages(
    make_actor, limited, sessionmaker
):
    owner, writer, room_id = await room_for(make_actor, limited, sessionmaker)
    await insert_messages(sessionmaker, room_id, writer.id, 3)
    await send(writer, room_id, "current window bucket")  # a recent bucket that must survive
    for hours_ago in (25, 26, 30, 48, 72):
        await execute(
            sessionmaker,
            "INSERT INTO message_write_buckets (room_id, user_id, window_start, count) "
            "VALUES (:r, :u, date_trunc('minute', clock_timestamp()) - make_interval(hours => :h), 3)",
            r=uuid.UUID(room_id),
            u=owner.id,
            h=hours_ago,
        )
    await execute(  # 23 h old: still within retention
        sessionmaker,
        "INSERT INTO message_write_buckets (room_id, user_id, window_start, count) "
        "VALUES (:r, :u, date_trunc('minute', clock_timestamp()) - interval '23 hours', 1)",
        r=uuid.UUID(room_id),
        u=owner.id,
    )
    messages_before = await rows(sessionmaker, "SELECT * FROM messages ORDER BY id")

    result = await cleanup_auth(sessionmaker, batch_size=2)  # five old rows, two per batch

    assert result.message_write_buckets == 5
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM message_write_buckets") == 2
    )  # 23 h + now
    assert await rows(sessionmaker, "SELECT * FROM messages ORDER BY id") == messages_before
    assert (await cleanup_auth(sessionmaker)).message_write_buckets == 0  # idempotent
