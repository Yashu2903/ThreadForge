"""Verifies the row-lock facts the design relies on (see app/services/rooms.py).

The PostgreSQL docs' conflict table says FOR KEY SHARE conflicts only with FOR UPDATE, and that
FOR NO KEY UPDATE conflicts with itself but not with FOR KEY SHARE. The docs page does not state
that inserting a referencing row (a membership or invitation) takes FOR KEY SHARE on the parent
room row; these tests establish that empirically on this server.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from tests.helpers import add_member, execute, scalar

LOCK_NOT_AVAILABLE = (OperationalError, DBAPIError)


async def make_room(sm):
    await execute(sm, "INSERT INTO users (display_name) VALUES ('owner'), ('guest')")
    owner = await scalar(sm, "SELECT id FROM users WHERE display_name = 'owner'")
    guest = await scalar(sm, "SELECT id FROM users WHERE display_name = 'guest'")
    await execute(sm, "INSERT INTO rooms (name, owner_id) VALUES ('r', :o)", o=owner)
    room = await scalar(sm, "SELECT id FROM rooms")
    return room, owner, guest


async def short_timeout(db, ms=400):
    await db.execute(text(f"SET LOCAL lock_timeout = '{ms}ms'"))


async def test_membership_insert_is_not_blocked_by_for_no_key_update_on_the_room(sessionmaker):
    room, _, guest = await make_room(sessionmaker)
    async with sessionmaker() as holder, holder.begin():
        await holder.execute(
            text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room}
        )
        # While the room is locked FOR NO KEY UPDATE, an FK insert (FOR KEY SHARE) proceeds.
        async with sessionmaker() as writer, writer.begin():
            await short_timeout(writer)
            await writer.execute(
                text("INSERT INTO memberships (room_id, user_id, role) VALUES (:r, :u, 'viewer')"),
                {"r": room, "u": guest},
            )
    assert await scalar(sessionmaker, "SELECT count(*) FROM memberships") == 1


async def test_for_update_on_the_room_WOULD_block_that_insert(sessionmaker):
    """The contrast that justifies FOR NO KEY UPDATE."""
    room, _, guest = await make_room(sessionmaker)
    async with sessionmaker() as holder, holder.begin():
        await holder.execute(text("SELECT 1 FROM rooms WHERE id = :r FOR UPDATE"), {"r": room})
        with pytest.raises(LOCK_NOT_AVAILABLE, match="lock timeout|LockNotAvailable"):
            async with sessionmaker() as writer, writer.begin():
                await short_timeout(writer)
                await writer.execute(
                    text(
                        "INSERT INTO memberships (room_id, user_id, role) VALUES (:r, :u, 'viewer')"
                    ),
                    {"r": room, "u": guest},
                )


async def test_two_room_mutators_exclude_each_other(sessionmaker):
    room, _, _ = await make_room(sessionmaker)
    async with sessionmaker() as first, first.begin():
        await first.execute(
            text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room}
        )
        with pytest.raises(LOCK_NOT_AVAILABLE, match="lock timeout|LockNotAvailable"):
            async with sessionmaker() as second, second.begin():
                await short_timeout(second)
                await second.execute(
                    text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room}
                )


async def test_second_mutator_proceeds_once_the_first_commits(sessionmaker):
    room, _, _ = await make_room(sessionmaker)
    order: list[str] = []
    first_has_lock, release_first = asyncio.Event(), asyncio.Event()

    async def first():
        async with sessionmaker() as db, db.begin():
            await db.execute(
                text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room}
            )
            first_has_lock.set()
            await release_first.wait()
            order.append("first-commit")

    async def second():
        await first_has_lock.wait()
        async with sessionmaker() as db, db.begin():
            await db.execute(
                text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room}
            )
            order.append("second-got-lock")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await first_has_lock.wait()
    await asyncio.sleep(0.2)  # give the second one a chance to (wrongly) get through
    assert order == []
    release_first.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert order == ["first-commit", "second-got-lock"]


async def test_membership_update_conflicts_with_a_for_share_check(sessionmaker):
    """Part 3 hook: a message-send check will SELECT ... FOR SHARE the membership row. Removal and
    role changes UPDATE that row, so they conflict with it (and vice versa)."""
    room, _, guest = await make_room(sessionmaker)
    await add_member(sessionmaker, room, guest, "editor")
    async with sessionmaker() as checker, checker.begin():
        await checker.execute(
            text("SELECT 1 FROM memberships WHERE room_id = :r AND user_id = :u FOR SHARE"),
            {"r": room, "u": guest},
        )
        with pytest.raises(LOCK_NOT_AVAILABLE, match="lock timeout|LockNotAvailable"):
            async with sessionmaker() as remover, remover.begin():
                await short_timeout(remover)
                await remover.execute(
                    text(
                        "UPDATE memberships SET state = 'removed', removed_at = now() "
                        "WHERE room_id = :r AND user_id = :u"
                    ),
                    {"r": room, "u": guest},
                )
