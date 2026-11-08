"""Races between message writes and membership changes (overlap forced behind the room lock)."""

import asyncio
import logging
import random
import re

import pytest
import pytest_asyncio

from tests.factories import make_settings
from tests.helpers import add_member, insert_messages, rows, scalar
from tests.test_invitations_api import new_room
from tests.test_members_api import member_path
from tests.test_messages_edit_delete import delete, edit, row
from tests.test_messages_send import send
from tests.test_rooms_concurrency import contended


@pytest_asyncio.fixture(autouse=True)
async def _start_early_in_the_minute(sessionmaker):
    """So the rate-limit bucket totals checked below cannot straddle a window rollover."""
    while True:
        left = float(
            await scalar(
                sessionmaker,
                "SELECT extract(epoch FROM date_trunc('minute', clock_timestamp())"
                " + interval '1 minute' - clock_timestamp())",
            )
        )
        if left >= 10:
            return
        await asyncio.sleep(left + 0.3)


@pytest.mark.parametrize("round_", range(4))
async def test_removal_versus_send_is_consistent_in_either_order(alice, bob, sessionmaker, round_):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    send_response, remove_response = await contended(
        sessionmaker,
        room_id,
        send(bob, room_id, "racing the removal"),
        alice.delete(member_path(room_id, bob)),
    )
    assert remove_response.status_code == 204
    stored = await scalar(sessionmaker, "SELECT count(*) FROM messages")
    if send_response.status_code == 201:  # committed before the removal: valid, and it stays
        assert stored == 1
    else:  # ordered after the removal: its in-transaction membership check failed
        assert (send_response.status_code, send_response.json()["error"]["code"]) == (
            404,
            "room_not_found",
        )
        assert stored == 0
    # either way bob is out afterwards
    assert (await send(bob, room_id, "after")).status_code == 404
    assert (await bob.get(f"/rooms/{room_id}/messages")).status_code == 404
    assert (
        await scalar(sessionmaker, "SELECT state FROM memberships WHERE user_id = :u", u=bob.id)
        == "removed"
    )


@pytest.mark.parametrize("round_", range(3))
async def test_removal_versus_edit_and_delete_by_the_removed_author(
    alice, bob, sessionmaker, round_
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    mine, doomed = await insert_messages(sessionmaker, room_id, bob.id, 2)
    edit_response, delete_response, remove_response = await contended(
        sessionmaker,
        room_id,
        edit(bob, room_id, mine, "edited at the last moment", 1),
        delete(bob, room_id, doomed, 1),
        alice.delete(member_path(room_id, bob)),
    )
    assert remove_response.status_code == 204
    assert edit_response.status_code in (200, 404) and delete_response.status_code in (204, 404)
    first = await row(sessionmaker, mine)
    second = await row(sessionmaker, doomed)
    # each outcome is internally consistent: succeeded <=> the change is in the database
    assert (first["version"] == 2) == (edit_response.status_code == 200)
    assert (second["deleted_at"] is not None) == (delete_response.status_code == 204)
    # and after the removal nothing further is possible
    assert (await edit(bob, room_id, mine, "too late", first["version"])).status_code == 404
    assert (await delete(bob, room_id, mine, first["version"])).status_code == 404


@pytest.mark.parametrize("round_", range(3))
@pytest.mark.parametrize(("start_role", "new_role"), [("editor", "viewer"), ("viewer", "editor")])
async def test_a_role_change_racing_a_send_never_affects_the_send(
    alice, bob, sessionmaker, round_, start_role, new_role
):
    """Role does not affect chat, so a role change racing a message write ends with BOTH
    succeeding, whichever the lock order (a downgrade and an upgrade behave the same)."""
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, start_role)
    send_response, patch_response = await contended(
        sessionmaker,
        room_id,
        send(bob, room_id, "racing the role change"),
        alice.patch(member_path(room_id, bob), json={"role": new_role}),
    )
    assert (send_response.status_code, patch_response.status_code) == (201, 200)
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == 1
    assert (
        await scalar(sessionmaker, "SELECT role FROM memberships WHERE user_id = :u", u=bob.id)
        == new_role
    )
    assert (await send(bob, room_id, "after the change")).status_code == 201  # still works


async def test_a_role_change_racing_a_send_both_orderings_are_deterministic(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    change = lambda role: alice.patch(member_path(room_id, bob), json={"role": role})  # noqa: E731

    # Ordering 1: the send is ahead of the downgrade.
    sent, changed = await ordered(
        sessionmaker, room_id, send(bob, room_id, "before"), change("viewer")
    )
    assert (sent.status_code, changed.status_code) == (201, 200)
    # Ordering 2: the upgrade is ahead of the send (bob is a viewer now).
    changed, sent = await ordered(
        sessionmaker, room_id, change("editor"), send(bob, room_id, "after")
    )
    assert (changed.status_code, sent.status_code) == (200, 201)
    # Ordering 3: the downgrade is ahead of the send: it still succeeds, as a viewer.
    changed, sent = await ordered(
        sessionmaker, room_id, change("viewer"), send(bob, room_id, "as viewer")
    )
    assert (changed.status_code, sent.status_code) == (200, 201)
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == 3


@pytest.mark.parametrize("round_", range(2))
async def test_a_role_change_racing_an_edit_or_delete_of_own_messages_never_blocks_them(
    alice, bob, sessionmaker, round_
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    mine, doomed = await insert_messages(sessionmaker, room_id, bob.id, 2)
    edit_response, delete_response, patch_response = await contended(
        sessionmaker,
        room_id,
        edit(bob, room_id, mine, "edited during the downgrade", 1),
        delete(bob, room_id, doomed, 1),
        alice.patch(member_path(room_id, bob), json={"role": "viewer"}),
    )
    assert (edit_response.status_code, delete_response.status_code, patch_response.status_code) == (
        200,
        204,
        200,
    )
    assert (await row(sessionmaker, mine))["version"] == 2
    assert (await row(sessionmaker, doomed))["deleted_at"] is not None


async def test_a_mixed_burst_of_message_and_membership_writes_keeps_every_invariant(
    make_actor, sessionmaker, caplog
):
    """~70 random message writes from several editors plus the owner (and one removal mid-burst),
    all queued behind the room lock. Independent oracle: the structured log emits exactly one line
    per COMMITTED write, keyed by message id, so each message's version must equal
    1 + (its logged edits and deletes) and the rate-limit buckets must total the logged writes."""
    caplog.set_level(logging.INFO, logger="threadforge.messages")
    roomy = make_settings(message_writes_per_minute=1000)
    owner = await make_actor("Owner", app_settings=roomy)
    editors = [await make_actor(f"Editor{i}", app_settings=roomy) for i in range(4)]
    room_id = await new_room(owner)
    for editor in editors:
        await add_member(sessionmaker, room_id, editor.id, "editor")
    owned = {
        e.id: await insert_messages(sessionmaker, room_id, e.id, 6, prefix=f"e{n}")
        for n, e in enumerate(editors)
    }
    caplog.clear()
    rng = random.Random(77)

    operations = []
    for i in range(70):
        editor = rng.choice(editors)
        kind = rng.choice(["send", "send", "edit", "edit", "delete", "owner_delete"])
        target = rng.choice(owned[editor.id])
        if kind == "send":
            operations.append(send(editor, room_id, f"burst {i}"))
        elif kind == "edit":
            operations.append(edit(editor, room_id, target, f"edited {i}", rng.choice([1, 1, 2])))
        elif kind == "delete":
            operations.append(delete(editor, room_id, target, rng.choice([1, 2])))
        else:  # owner_delete
            operations.append(delete(owner, room_id, target, rng.choice([1, 2])))
    operations.insert(35, owner.delete(member_path(room_id, editors[3])))  # a removal mid-burst

    sem = asyncio.Semaphore(16)

    async def guarded(awaitable):
        async with sem:
            return await awaitable

    results = await contended(
        sessionmaker, room_id, *[guarded(op) for op in operations], settle=1.0
    )
    assert not [r for r in results if r.status_code >= 500], (
        "an operation failed (deadlock/timeout?)"
    )

    logged = [r.getMessage() for r in caplog.records if r.name == "threadforge.messages"]
    count = lambda action: sum(f"action={action} " in line for line in logged)  # noqa: E731
    sent, edited, deleted = count("message_sent"), count("message_edited"), count("message_deleted")
    assert sent > 5 and edited > 3 and deleted > 3, (sent, edited, deleted)

    total_messages = await scalar(sessionmaker, "SELECT count(*) FROM messages")
    assert total_messages == 4 * 6 + sent  # every logged send is one row, nothing else appeared
    assert (
        await scalar(sessionmaker, "SELECT coalesce(sum(count), 0) FROM message_write_buckets")
        == sent + edited + deleted
    )

    per_message: dict[str, int] = {}
    for line in logged:
        match = re.search(r"action=message_(edited|deleted) .*message_id=([0-9a-f-]{36})", line)
        if match:
            per_message[match.group(2)] = per_message.get(match.group(2), 0) + 1
    for message in await rows(sessionmaker, "SELECT id, version, body, deleted_at FROM messages"):
        assert message["version"] == 1 + per_message.get(str(message["id"]), 0)
        assert (message["body"] is None) == (
            message["deleted_at"] is not None
        )  # tombstone <=> no body
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL")
        == deleted
    )


async def test_reads_take_no_lock_and_are_not_blocked_by_a_held_room_lock(alice, bob, sessionmaker):
    """GET history and GET one message take no locks: they complete while another transaction
    holds the room lock (a write, by contrast, queues behind it)."""
    from sqlalchemy import text

    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    (message_id,) = await insert_messages(sessionmaker, room_id, alice.id, 1)
    async with sessionmaker() as holder, holder.begin():
        await holder.execute(
            text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room_id}
        )
        reads = await asyncio.wait_for(
            asyncio.gather(
                bob.get(f"/rooms/{room_id}/messages"),
                alice.get(f"/rooms/{room_id}/messages/{message_id}"),
            ),
            timeout=2,
        )
        assert [r.status_code for r in reads] == [200, 200]
        blocked_write = asyncio.create_task(send(alice, room_id, "waits for the lock"))
        await asyncio.sleep(0.5)
        assert not blocked_write.done()  # the write is queued behind the held lock
    assert (await asyncio.wait_for(blocked_write, timeout=5)).status_code == 201


async def ordered(sessionmaker, room_id, first, second):
    """Queue `first` then `second` behind a held room lock (lock waiters are served in queue
    order), then release. Gives a DETERMINISTIC ordering of two racing requests."""
    from sqlalchemy import text

    async with sessionmaker() as holder, holder.begin():
        await holder.execute(
            text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room_id}
        )
        task_first = asyncio.create_task(first)
        await asyncio.sleep(0.4)  # first is now waiting at the lock
        task_second = asyncio.create_task(second)
        await asyncio.sleep(0.4)
        assert not task_first.done() and not task_second.done()
    return await asyncio.wait_for(asyncio.gather(task_first, task_second), timeout=10)


@pytest.mark.parametrize("role", ["editor", "viewer"])
async def test_removal_versus_send_both_orderings_are_deterministic(alice, bob, sessionmaker, role):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, role)

    # Ordering 1: the send is ahead of the removal -> it commits, and the message stays.
    send_response, remove_response = await ordered(
        sessionmaker,
        room_id,
        send(bob, room_id, "ahead of the removal"),
        alice.delete(member_path(room_id, bob)),
    )
    assert (send_response.status_code, remove_response.status_code) == (201, 204)
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == 1

    # Ordering 2 (bob rejoins first): the removal is ahead of the send -> the send fails its
    # in-transaction membership check and writes nothing.
    await add_member(sessionmaker, room_id, bob.id, role)
    remove_response, send_response = await ordered(
        sessionmaker,
        room_id,
        alice.delete(member_path(room_id, bob)),
        send(bob, room_id, "behind the removal"),
    )
    assert (remove_response.status_code, send_response.status_code) == (204, 404)
    assert send_response.json()["error"]["code"] == "room_not_found"
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == 1  # nothing new written
