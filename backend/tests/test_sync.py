"""GET /rooms/{room_id}/sync: one consistent snapshot of state + through_seq."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.services import memberships
from app.services.sync import build_sync, open_snapshot
from tests.helpers import add_member, execute, insert_messages, rows, scalar
from tests.test_invitations_api import new_room
from tests.test_messages_edit_delete import delete, edit
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio


async def test_sync_returns_state_and_through_seq_together(alice, bob, sessionmaker):
    room_id = await new_room(alice, "Planning")
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    sent = (await send(alice, room_id, "one")).json()
    await send(bob, room_id, "two")  # a viewer can chat
    await delete(alice, room_id, sent["id"], 1)
    response = await bob.get(f"/rooms/{room_id}/sync")
    assert response.status_code == 200
    body = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert body["room"]["id"] == room_id and body["room"]["name"] == "Planning"
    assert body["room"]["owner_id"] == str(alice.id) and body["role"] == "viewer"
    assert set(body["room"]) == {
        "id", "name", "owner_id", "created_at", "updated_at",
        "active_document_id", "active_document_version",
    }  # fmt: skip
    assert sorted(m["role"] for m in body["members"]) == ["owner", "viewer"]
    assert set(body["members"][0]) == {"user_id", "display_name", "role", "joined_at"}  # no email
    assert [m["body"] for m in body["messages"]] == [None, "two"]  # tombstone, then live; ascending
    assert body["messages"][0]["is_deleted"] is True
    assert body["membership_epoch"] >= 1
    assert body["through_seq"] == await scalar(
        sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id
    )
    events = (await bob.get(f"/rooms/{room_id}/events")).json()
    assert body["through_seq"] == events["current_room_seq"]


async def test_sync_is_bounded_and_next_before_pages_older_messages(alice, sessionmaker):
    room_id = await new_room(alice)
    await insert_messages(sessionmaker, room_id, alice.id, 60)
    body = (await alice.get(f"/rooms/{room_id}/sync")).json()
    assert len(body["messages"]) == 50 and body["next_before"] is not None
    older = (
        await alice.get(f"/rooms/{room_id}/messages", params={"before": body["next_before"]})
    ).json()
    newest_ids = {m["id"] for m in body["messages"]}
    assert len(older["items"]) == 10 and not newest_ids & {m["id"] for m in older["items"]}


async def test_room_without_events_has_through_seq_zero(alice, sessionmaker):
    """A room created before Part 4 has event_seq 0 and no events; its messages still come from
    the bootstrap."""
    await execute(sessionmaker, "INSERT INTO rooms (name, owner_id) VALUES ('old', :o)", o=alice.id)
    room_id = await scalar(sessionmaker, "SELECT id FROM rooms WHERE name = 'old'")
    await add_member(sessionmaker, room_id, alice.id, "owner")
    await insert_messages(sessionmaker, room_id, alice.id, 3)
    body = (await alice.get(f"/rooms/{room_id}/sync")).json()
    assert body["through_seq"] == 0 and len(body["messages"]) == 3


async def test_nonmember_removed_member_and_unknown_room_are_404(alice, bob, sessionmaker, client):
    room_id = await new_room(alice)
    assert (await bob.get(f"/rooms/{room_id}/sync")).status_code == 404
    await add_member(sessionmaker, room_id, bob.id, "editor", state="removed")
    removed = await bob.get(f"/rooms/{room_id}/sync")
    unknown = await bob.get(f"/rooms/{uuid.uuid4()}/sync")
    assert removed.status_code == unknown.status_code == 404
    assert removed.json()["error"]["code"] == unknown.json()["error"]["code"] == "room_not_found"
    assert (await bob.get("/rooms/not-a-uuid/sync")).status_code == 404
    assert (await client.get(f"/rooms/{room_id}/sync")).status_code == 401


async def test_member_list_is_bounded_by_the_member_cap(alice, bob, carol, sessionmaker, settings):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id)
    await add_member(sessionmaker, room_id, carol.id)
    small = settings.model_copy(update={"max_members_per_room": 2})
    out = await build_sync(sessionmaker, small, uuid.UUID(room_id), alice.id)
    assert len(out.members) == 2


# ---- snapshot properties --------------------------------------------------------------------------


async def test_snapshot_is_repeatable_read_and_read_only(sessionmaker):
    async with open_snapshot(sessionmaker) as db:
        isolation = (await db.execute(text("SHOW transaction_isolation"))).scalar_one()
        read_only = (await db.execute(text("SHOW transaction_read_only"))).scalar_one()
        assert (isolation, read_only) == ("repeatable read", "on")
        with pytest.raises(DBAPIError):
            await db.execute(text("UPDATE rooms SET name = name"))


async def test_isolation_cannot_be_changed_after_the_first_statement(sessionmaker):
    """Why the snapshot uses its own session: the request session has already run queries."""
    async with sessionmaker() as db:
        await db.execute(text("SELECT 1"))
        with pytest.raises(DBAPIError, match="before any query"):
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


async def test_a_write_between_reads_never_mixes_into_one_sync(
    alice, bob, sessionmaker, settings, monkeypatch
):
    """Commit a message AND a new member between the snapshot's reads: the response must show
    neither, and through_seq must match what it shows. The same interleaving on a plain
    READ COMMITTED session DOES mix (proving the test is not vacuous)."""
    room_id = await new_room(alice)
    await send(alice, room_id, "before")
    original = memberships.fetch_active_members

    async def mutate_then_read(db, room, cap):
        await send(alice, room_id, "committed mid-snapshot")
        await add_member(sessionmaker, room_id, bob.id, "editor")
        return await original(db, room, cap)

    monkeypatch.setattr(memberships, "fetch_active_members", mutate_then_read)
    out = await build_sync(sessionmaker, settings, uuid.UUID(room_id), alice.id)
    assert [m.body for m in out.messages] == ["before"]
    assert [m.role for m in out.members] == ["owner"]
    assert out.through_seq == 2  # room_created + the first message; the mid-snapshot send is seq 3
    now = await scalar(sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id)
    assert now == 3  # the mutation really committed

    # contrast: READ COMMITTED sees the new message on a later statement of the same transaction
    async with sessionmaker() as db:
        first = (
            await db.execute(text("SELECT event_seq FROM rooms WHERE id = :r"), {"r": room_id})
        ).scalar_one()
        await send(alice, room_id, "after first read")
        count = (
            await db.execute(
                text("SELECT count(*) FROM messages WHERE room_id = :r"), {"r": room_id}
            )
        ).scalar_one()
    # event_seq (3) accounts for room_created + 2 messages, but the later statement already sees
    # a THIRD message: under READ COMMITTED the data would run ahead of through_seq.
    assert first == 3 and count == (first - 1) + 1


async def test_edit_then_sync_shows_new_version_and_matching_seq(alice, sessionmaker):
    room_id = await new_room(alice)
    created = (await send(alice, room_id, "v1")).json()
    await edit(alice, room_id, created["id"], "v2", 1)
    body = (await alice.get(f"/rooms/{room_id}/sync")).json()
    assert [(m["body"], m["version"]) for m in body["messages"]] == [("v2", 2)]
    events = await rows(
        sessionmaker, "SELECT type FROM room_events WHERE room_id = :r ORDER BY seq", r=room_id
    )
    assert body["through_seq"] == len(events) == 3
