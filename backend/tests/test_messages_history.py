"""GET history and GET one message: access, ordering, pagination, cursors, query count."""

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from app.services import messages as messages_service
from tests.helpers import add_member, execute, insert_messages, rows, scalar
from tests.test_invitations_api import new_room
from tests.test_messages_send import send
from tests.test_rooms_concurrency import contended


async def history(actor, room_id, **params):
    return await actor.get(f"/rooms/{room_id}/messages", params=params)


async def walk_older(actor, room_id, limit):
    """Follow next_before to the end; returns (all items oldest-first per page order, pages)."""
    pages, cursor = [], None
    while True:
        params = {"limit": limit, **({"before": cursor} if cursor else {})}
        body = (await history(actor, room_id, **params)).json()
        pages.append([m["id"] for m in body["items"]])
        cursor = body["next_before"]
        if cursor is None:
            return pages


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).rstrip(b"=").decode()


# ---- access ---------------------------------------------------------------------------------------


async def test_every_active_role_can_read_and_outsiders_cannot(
    alice, bob, carol, make_actor, client, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    editor = await make_actor("Editor")
    await add_member(sessionmaker, room_id, editor.id, "editor")
    removed = await make_actor("Removed")
    await add_member(sessionmaker, room_id, removed.id, "editor", "removed")
    sent = (await send(alice, room_id, "hello")).json()

    for reader in (alice, editor, bob):  # owner, editor, viewer
        response = await history(reader, room_id)
        assert response.status_code == 200
        assert [m["id"] for m in response.json()["items"]] == [sent["id"]]
        assert response.headers["cache-control"] == "no-store"
        single = await reader.get(f"/rooms/{room_id}/messages/{sent['id']}")
        assert (single.status_code, single.json()["body"]) == (200, "hello")
    for outsider in (removed, carol):
        for path in (f"/rooms/{room_id}/messages", f"/rooms/{room_id}/messages/{sent['id']}"):
            response = await outsider.get(path)
            assert (response.status_code, response.json()["error"]["code"]) == (
                404,
                "room_not_found",
            )
    assert (await client.get(f"/rooms/{room_id}/messages")).status_code == 401
    assert (await client.get(f"/rooms/{room_id}/messages/{sent['id']}")).status_code == 401


async def test_history_is_room_scoped_and_cross_room_ids_are_404(alice, sessionmaker):
    room_a, room_b = await new_room(alice, "A"), await new_room(alice, "B")
    in_a = (await send(alice, room_a, "in A")).json()
    in_b = (await send(alice, room_b, "in B")).json()
    assert [m["body"] for m in (await history(alice, room_a)).json()["items"]] == ["in A"]
    assert [m["body"] for m in (await history(alice, room_b)).json()["items"]] == ["in B"]
    # alice belongs to both rooms, but a message id from room B is not reachable through room A
    for path, expected in (
        (f"/rooms/{room_a}/messages/{in_b['id']}", 404),
        (f"/rooms/{room_b}/messages/{in_a['id']}", 404),
    ):
        response = await alice.get(path)
        assert (response.status_code, response.json()["error"]["code"]) == (
            expected,
            "message_not_found",
        )


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "0" * 32, "00000000-0000-0000-0000-000000000009"])
async def test_unknown_or_malformed_message_ids_are_message_not_found(alice, bad_id):
    room_id = await new_room(alice)
    response = await alice.get(f"/rooms/{room_id}/messages/{bad_id}")
    assert (response.status_code, response.json()["error"]["code"]) == (404, "message_not_found")


async def test_complete_history_is_visible_regardless_of_when_a_member_joined(
    alice, bob, carol, sessionmaker
):
    """No per-member visibility window: messages from before joining, from while removed, and from
    since-removed authors are all visible to every active member."""
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    before_join = (await send(alice, room_id, "before carol joined")).json()
    by_bob = (await send(bob, room_id, "bob wrote this")).json()
    await add_member(sessionmaker, room_id, bob.id, "editor", "removed")  # bob leaves...
    while_removed = (await send(alice, room_id, "while bob was removed")).json()
    await add_member(sessionmaker, room_id, carol.id, "viewer")  # ...carol joins late
    seen = [m["id"] for m in (await history(carol, room_id)).json()["items"]]
    assert seen == [before_join["id"], by_bob["id"], while_removed["id"]]
    await add_member(sessionmaker, room_id, bob.id, "viewer")  # bob rejoins
    assert [m["id"] for m in (await history(bob, room_id)).json()["items"]] == seen  # incl. his own
    assert (await history(alice, room_id)).json()["items"][1]["author_display_name"] == "Bob"


# ---- ordering and pagination --------------------------------------------------------------------------


async def test_first_page_is_the_newest_messages_in_ascending_order(alice, sessionmaker):
    room_id = await new_room(alice)
    ids = await insert_messages(sessionmaker, room_id, alice.id, 7)
    body = (await history(alice, room_id, limit=3)).json()
    assert [m["id"] for m in body["items"]] == [str(i) for i in ids[-3:]]  # newest 3, oldest first
    stamps = [m["created_at"] for m in body["items"]]
    assert stamps == sorted(stamps) and body["next_before"] is not None


async def test_walking_older_pages_has_no_duplicates_or_gaps(alice, sessionmaker):
    room_id = await new_room(alice)
    ids = [str(i) for i in await insert_messages(sessionmaker, room_id, alice.id, 23)]
    pages = await walk_older(alice, room_id, limit=5)
    assert [len(p) for p in pages] == [5, 5, 5, 5, 3]
    collected = [mid for page in reversed(pages) for mid in page]  # oldest page first
    assert collected == ids and len(set(collected)) == 23


async def test_identical_timestamps_are_ordered_by_id_and_paginate_across_a_boundary(
    alice, sessionmaker
):
    room_id = await new_room(alice)
    await insert_messages(sessionmaker, room_id, alice.id, 11, step=timedelta(0))  # all equal
    assert await scalar(sessionmaker, "SELECT count(DISTINCT created_at) FROM messages") == 1
    expected = [
        str(r["id"])
        for r in await rows(sessionmaker, "SELECT id FROM messages ORDER BY created_at, id")
    ]
    pages = await walk_older(alice, room_id, limit=4)  # the page boundary falls inside the tie
    assert [mid for page in reversed(pages) for mid in page] == expected
    assert len({mid for page in pages for mid in page}) == 11


async def test_exact_page_boundary_has_no_phantom_older_page(alice, sessionmaker):
    room_id = await new_room(alice)
    await insert_messages(sessionmaker, room_id, alice.id, 4)
    body = (await history(alice, room_id, limit=4)).json()
    assert len(body["items"]) == 4 and body["next_before"] is None


async def test_default_limit_is_50_and_empty_rooms_are_empty(alice, sessionmaker):
    room_id = await new_room(alice)
    assert (await history(alice, room_id)).json() == {"items": [], "next_before": None}
    await insert_messages(sessionmaker, room_id, alice.id, 55)
    body = (await history(alice, room_id)).json()
    assert len(body["items"]) == 50 and body["next_before"] is not None


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", ""])
async def test_invalid_limits_are_validation_errors(alice, limit):
    room_id = await new_room(alice)
    response = await alice.get(f"/rooms/{room_id}/messages", params={"limit": limit})
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")


async def test_limit_100_is_allowed(alice):
    room_id = await new_room(alice)
    assert (await history(alice, room_id, limit=100)).status_code == 200


async def test_history_includes_tombstones_without_text(alice, sessionmaker):
    room_id = await new_room(alice)
    await insert_messages(sessionmaker, room_id, alice.id, 2, prefix="visible")
    await execute(
        sessionmaker,
        "INSERT INTO messages (room_id, author_id, client_message_id, body, initial_request_fingerprint,"
        " created_at, deleted_at, deleted_by, version)"
        " VALUES (:r, :a, gen_random_uuid(), NULL, decode(repeat('ab', 32), 'hex'),"
        " '2026-06-01T00:00:00Z', '2026-06-01T00:01:00Z', :a, 2)",
        r=room_id,
        a=alice.id,
    )
    items = (await history(alice, room_id)).json()["items"]
    tombstone = items[-1]
    assert (tombstone["body"], tombstone["is_deleted"], tombstone["version"]) == (None, True, 2)
    assert tombstone["deleted_by"] == str(alice.id) and tombstone["deleted_at"] is not None
    assert [m["is_deleted"] for m in items] == [False, False, True]
    single = (await alice.get(f"/rooms/{room_id}/messages/{tombstone['id']}")).json()
    assert single["body"] is None and single["is_deleted"] is True


async def test_author_display_name_is_read_at_query_time_with_a_deleted_user_fallback(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    sent = (await send(bob, room_id, "hi")).json()
    assert sent["author_display_name"] == "Bob"
    await execute(sessionmaker, "UPDATE users SET display_name = 'Robert' WHERE id = :u", u=bob.id)
    assert (await history(alice, room_id)).json()["items"][0]["author_display_name"] == "Robert"
    await execute(sessionmaker, "UPDATE users SET state = 'deleted' WHERE id = :u", u=bob.id)
    item = (await history(alice, room_id)).json()["items"][0]
    assert item["author_display_name"] == "Deleted user"
    assert item["author_id"] == str(bob.id)  # the id stays; only the display name is replaced


async def test_responses_carry_no_fingerprint_or_private_fields(alice):
    room_id = await new_room(alice)
    await send(alice, room_id, "x")
    text = (await history(alice, room_id)).text.lower()
    for forbidden in ("fingerprint", "token_hash", "secret", "email", "session"):
        assert forbidden not in text


# ---- cursors -------------------------------------------------------------------------------------------


async def real_cursor(alice, sessionmaker, room_id):
    await insert_messages(sessionmaker, room_id, alice.id, 6)
    return (await history(alice, room_id, limit=2)).json()["next_before"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c, room, other: "garbage!",
        lambda c, room, other: "",
        lambda c, room, other: "A" * 400,
        lambda c, room, other: b64("not a cursor"),
        lambda c, room, other: c + "=",  # padded: non-canonical
        lambda c, room, other: c + "A",
        lambda c, room, other: c[:-4],
        lambda c, room, other: b64(
            base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)).decode().replace("m1|", "m2|", 1)
        ),  # wrong version
        lambda c, room, other: b64(
            base64.urlsafe_b64decode(c + "=" * (-len(c) % 4))
            .decode()
            .replace(str(room), str(other))
        ),  # a cursor naming a different room
        lambda c, room, other: b64(
            f"m1|{room}|2026-01-01T00:00:00+00:00|{uuid.uuid4()}"
        ),  # no micros
        lambda c, room, other: b64(f"m1|{room}|2026-01-01T00:00:00.000000+02:00|{uuid.uuid4()}"),
        lambda c, room, other: b64(
            f"m1|{room}|2026-01-01T00:00:00.000000+00:00|{str(uuid.uuid4()).upper()}"
        ),
    ],
)
async def test_malformed_wrong_version_and_cross_room_cursors_are_invalid(
    alice, sessionmaker, mutate
):
    room_id, other_room = await new_room(alice, "A"), await new_room(alice, "B")
    cursor = await real_cursor(alice, sessionmaker, room_id)
    response = await alice.get(
        f"/rooms/{room_id}/messages", params={"before": mutate(cursor, room_id, other_room)}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_cursor")


async def test_a_cursor_from_one_room_does_not_work_in_another(alice, sessionmaker):
    room_a, room_b = await new_room(alice, "A"), await new_room(alice, "B")
    cursor = await real_cursor(alice, sessionmaker, room_a)
    await insert_messages(sessionmaker, room_b, alice.id, 3)
    response = await alice.get(f"/rooms/{room_b}/messages", params={"before": cursor})
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_cursor")


async def test_authorization_is_checked_before_the_cursor(alice, bob):
    room_id = await new_room(alice)
    response = await bob.get(f"/rooms/{room_id}/messages", params={"before": "garbage!"})
    assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")


async def test_a_forged_cursor_only_moves_the_position_inside_a_readable_room(
    alice, bob, sessionmaker
):
    """The cursor is unsigned: a forged one is just a position. It grants no access to anything
    the caller could not already read, and an outsider is still refused."""
    room_id, secret_room = await new_room(alice, "mine"), await new_room(alice, "private")
    await insert_messages(sessionmaker, room_id, alice.id, 3)
    forged = b64(f"m1|{room_id}|2030-01-01T00:00:00.000000+00:00|{uuid.uuid4()}")
    assert (
        len(
            (await alice.get(f"/rooms/{room_id}/messages", params={"before": forged})).json()[
                "items"
            ]
        )
        == 3
    )
    assert (
        await bob.get(f"/rooms/{room_id}/messages", params={"before": forged})
    ).status_code == 404
    assert secret_room != room_id


async def test_history_is_not_a_frozen_snapshot(alice, sessionmaker):
    room_id = await new_room(alice)
    await insert_messages(sessionmaker, room_id, alice.id, 6)
    first = (await history(alice, room_id, limit=3)).json()
    newer = (await send(alice, room_id, "arrived between requests")).json()
    older = (await history(alice, room_id, limit=3, before=first["next_before"])).json()
    assert newer["id"] not in {m["id"] for m in older["items"]}  # paging back is unaffected
    refreshed = (await history(alice, room_id, limit=3)).json()
    assert refreshed["items"][-1]["id"] == newer["id"]  # a fresh newest-page fetch sees it


# ---- constant query count --------------------------------------------------------------------------------


async def test_the_number_of_queries_for_a_page_does_not_grow_with_the_messages(
    alice, engine, sessionmaker
):
    room_id = await new_room(alice)
    statements = []

    def count(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)

    async def statements_for_one_page():
        statements.clear()
        event.listen(engine.sync_engine, "before_cursor_execute", count)
        try:
            response = await history(alice, room_id, limit=50)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count)
        assert response.status_code == 200
        return len(statements), len(response.json()["items"])

    await insert_messages(sessionmaker, room_id, alice.id, 3, prefix="few")
    few_queries, few_items = await statements_for_one_page()
    await insert_messages(
        sessionmaker, room_id, alice.id, 60, prefix="many", start=datetime(2026, 2, 1, tzinfo=UTC)
    )
    many_queries, many_items = await statements_for_one_page()
    assert (few_items, many_items) == (3, 50)
    assert few_queries == many_queries  # no per-message queries (no N+1)
    assert many_queries <= 6  # session lookup + membership pre-check + one joined history query


# ---- timestamps follow commit order ------------------------------------------------------------------------


async def test_a_message_committed_later_never_has_an_earlier_created_at(
    alice, make_actor, sessionmaker, monkeypatch
):
    """Race several concurrent senders, all queued behind the room lock. The order in which they
    obtain the lock IS the commit order (writes in a room are serialized); created_at, read from
    clock_timestamp() after the lock, must be non-decreasing in that order."""
    room_id = await new_room(alice)
    senders = [alice]
    for i in range(7):
        editor = await make_actor(f"Sender{i}")
        await add_member(sessionmaker, room_id, editor.id, "editor")
        senders.append(editor)

    lock_order: list = []
    real = messages_service._authorize_member

    async def recording(db, room, actor):
        locked_and_role = await real(db, room, actor)
        lock_order.append(actor)  # recorded while the room lock is held
        return locked_and_role

    monkeypatch.setattr(messages_service, "_authorize_member", recording)

    for round_ in range(3):
        lock_order.clear()
        results = await contended(
            sessionmaker, room_id, *[send(s, room_id, f"round {round_}") for s in senders]
        )
        assert [r.status_code for r in results] == [201] * 8
        stored = await rows(
            sessionmaker,
            "SELECT author_id, created_at FROM messages WHERE body = :b ORDER BY created_at, id",
            b=f"round {round_}",
        )
        assert [r["author_id"] for r in stored] == lock_order  # timestamp order == commit order
        stamps = [r["created_at"] for r in stored]
        assert stamps == sorted(stamps)
