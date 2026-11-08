"""PATCH and DELETE of messages: permission matrix, the documented check order, version conflicts."""

import uuid

import pytest

from tests.helpers import add_member, execute, rows, scalar
from tests.test_invitations_api import new_room
from tests.test_messages_send import send
from tests.test_rooms_concurrency import contended


async def edit(actor, room_id, message_id, body, version, **kw):
    return await actor.patch(
        f"/rooms/{room_id}/messages/{message_id}",
        json={"body": body, "expected_version": version},
        **kw,
    )


async def delete(actor, room_id, message_id, version, **kw):
    return await actor.delete(
        f"/rooms/{room_id}/messages/{message_id}", params={"expected_version": version}, **kw
    )


async def row(sm, message_id):
    [r] = await rows(sm, "SELECT * FROM messages WHERE id = :i", i=message_id)
    return r


async def setup_room(alice, bob, carol, sm):
    """alice = owner, bob = editor, carol = viewer. Returns the room id."""
    room_id = await new_room(alice)
    await add_member(sm, room_id, bob.id, "editor")
    await add_member(sm, room_id, carol.id, "viewer")
    return room_id


async def bucket_total(sm):
    return await scalar(sm, "SELECT coalesce(sum(count), 0) FROM message_write_buckets")


# ---- editing: success and permissions ---------------------------------------------------------------


async def test_author_edits_with_the_current_version(alice, bob, carol, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "first draft")).json()
    response = await edit(bob, room_id, sent["id"], "second draft", 1)
    assert response.status_code == 200
    body = response.json()
    assert (body["body"], body["version"], body["id"]) == ("second draft", 2, sent["id"])
    assert body["edited_at"] is not None and body["created_at"] == sent["created_at"]
    assert body["edited_at"] >= body["created_at"]
    stored = await row(sessionmaker, uuid.UUID(sent["id"]))
    assert (stored["body"], stored["version"]) == (
        "second draft",
        2,
    )  # overwritten: no history kept


async def test_owner_can_edit_their_own_message(alice, bob, carol, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(alice, room_id, "mine")).json()
    assert (await edit(alice, room_id, sent["id"], "mine, edited", 1)).status_code == 200


async def test_owner_cannot_edit_another_persons_message(alice, bob, carol, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "bob's words")).json()
    response = await edit(alice, room_id, sent["id"], "rewritten by the owner", 1)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "message_not_author")
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] == "bob's words"


async def test_an_editor_cannot_edit_another_editors_message(alice, bob, make_actor, sessionmaker):
    room_id = await new_room(alice)
    other = await make_actor("OtherEditor")
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await add_member(sessionmaker, room_id, other.id, "editor")
    sent = (await send(other, room_id, "theirs")).json()
    response = await edit(bob, room_id, sent["id"], "mine now", 1)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "message_not_author")


async def test_an_author_who_was_downgraded_can_still_edit_but_a_removed_one_cannot(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "written as editor")).json()
    await add_member(sessionmaker, room_id, bob.id, "viewer")  # downgraded: chat is unaffected
    response = await edit(bob, room_id, sent["id"], "edited as a viewer", 1)
    assert (response.status_code, response.json()["version"]) == (200, 2)
    await add_member(sessionmaker, room_id, bob.id, "editor")  # upgraded again: same behaviour
    assert (await edit(bob, room_id, sent["id"], "edited as an editor", 2)).status_code == 200
    await add_member(sessionmaker, room_id, bob.id, "editor", "removed")  # removal DOES block
    response = await edit(bob, room_id, sent["id"], "sneaky", 3)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] == "edited as an editor"


async def test_a_viewer_can_edit_their_own_message_but_not_anothers(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)  # carol is a viewer
    mine = (await send(carol, room_id, "viewer's first draft")).json()
    theirs = (await send(bob, room_id, "the editor's message")).json()
    response = await edit(carol, room_id, mine["id"], "viewer's second draft", 1)
    assert (response.status_code, response.json()["body"], response.json()["version"]) == (
        200,
        "viewer's second draft",
        2,
    )
    assert response.json()["edited_at"] is not None
    other = await edit(carol, room_id, theirs["id"], "rewritten by a viewer", 1)
    assert (other.status_code, other.json()["error"]["code"]) == (403, "message_not_author")
    assert (await row(sessionmaker, uuid.UUID(theirs["id"])))["body"] == "the editor's message"


async def test_outsiders_cannot_edit(alice, bob, carol, make_actor, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "x")).json()
    outsider = await make_actor("Outsider")
    response = await edit(outsider, room_id, sent["id"], "y", 1)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")


async def test_unknown_malformed_and_cross_room_message_ids_are_message_not_found(
    alice, sessionmaker
):
    room_a, room_b = await new_room(alice, "A"), await new_room(alice, "B")
    in_b = (await send(alice, room_b, "in B")).json()
    for target in (str(uuid.uuid4()), "not-a-uuid", in_b["id"]):  # the last is another room's id
        response = await edit(alice, room_a, target, "x", 1)
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "message_not_found",
        )
    assert (await row(sessionmaker, uuid.UUID(in_b["id"])))["body"] == "in B"


# ---- editing: version conflicts, no-ops, tombstones ----------------------------------------------------


async def test_stale_version_is_a_conflict_with_the_current_version(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "v1")).json()
    await edit(bob, room_id, sent["id"], "v2", 1)
    response = await edit(bob, room_id, sent["id"], "from an old tab", 1)  # stale
    assert response.status_code == 409
    error = response.json()["error"]
    assert (error["code"], error["current_version"]) == ("message_version_conflict", 2)
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] == "v2"
    ahead = await edit(bob, room_id, sent["id"], "from the future", 99)
    assert ahead.status_code == 409 and ahead.json()["error"]["current_version"] == 2


async def test_a_stale_version_conflicts_even_when_the_text_matches(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "v1")).json()
    await edit(bob, room_id, sent["id"], "v2", 1)
    response = await edit(bob, room_id, sent["id"], "v2", 1)  # same text as now, but stale version
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "message_version_conflict",
    )


async def test_an_edit_with_identical_text_and_current_version_changes_nothing(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "unchanged")).json()
    before, buckets = (
        await row(sessionmaker, uuid.UUID(sent["id"])),
        await bucket_total(sessionmaker),
    )
    response = await edit(bob, room_id, sent["id"], "unchanged", 1)
    assert response.status_code == 200 and response.json() == sent
    assert await row(sessionmaker, uuid.UUID(sent["id"])) == before  # no version, no edited_at
    assert await bucket_total(sessionmaker) == buckets  # no rate-limit capacity used


async def test_editing_a_tombstone_is_message_deleted(alice, bob, carol, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "to be deleted")).json()
    assert (await delete(bob, room_id, sent["id"], 1)).status_code == 204
    for version in (1, 2):  # whatever version the client claims
        response = await edit(bob, room_id, sent["id"], "resurrect", version)
        assert (response.status_code, response.json()["error"]["code"]) == (409, "message_deleted")
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"body": "x"},
        {"expected_version": 1},
        {"body": "x", "expected_version": 0},
        {"body": "x", "expected_version": -3},
        {"body": "x", "expected_version": "1"},
        {"body": "x", "expected_version": 1.0},
        {"body": "x", "expected_version": 1.5},
        {"body": "x", "expected_version": True},
        {"body": "x", "expected_version": None},
        {"body": "x", "expected_version": 2**63},  # would overflow bigint (a 500 if unchecked)
        {"body": "", "expected_version": 1},
        {"body": "   ", "expected_version": 1},
        {"body": "​", "expected_version": 1},
        {"body": 5, "expected_version": 1},
        {"body": "x" * 5001, "expected_version": 1},
        {"body": "x", "expected_version": 1, "author_id": str(uuid.uuid4())},
        {"body": "x", "expected_version": 1, "client_message_id": str(uuid.uuid4())},
        {"body": "x", "expected_version": 1, "version": 5},
        {"body": "x", "expected_version": 1, "deleted_at": None},
    ],
)
async def test_invalid_edit_requests_are_422_and_change_nothing(
    alice, bob, carol, sessionmaker, payload
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "keep me")).json()
    response = await bob.patch(f"/rooms/{room_id}/messages/{sent['id']}", json=payload)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")
    stored = await row(sessionmaker, uuid.UUID(sent["id"]))
    assert (stored["body"], stored["version"]) == ("keep me", 1)


async def test_edit_gate_order(alice, bob, carol, make_actor, client, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "x")).json()
    path = f"/rooms/{room_id}/messages/{sent['id']}"
    outsider = await make_actor("Outsider")
    bad = {"body": "", "expected_version": 0}
    assert (await client.patch(path, json=bad)).status_code == 401
    assert (await bob.patch(path, json=bad, csrf=False)).json()["error"]["code"] == "csrf_failed"
    assert (await outsider.patch(path, json=bad)).status_code == 404  # outsider + bad body
    # any ACTIVE member, viewer included, passes the access gate and reaches validation
    assert (await carol.patch(path, json=bad)).status_code == 422  # viewer + bad body
    assert (
        await bob.patch(path, json=bad)
    ).status_code == 422  # a member finally reaches validation


async def test_edit_then_resend_returns_the_edited_representation(alice, bob, carol, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    cmid = str(uuid.uuid4())
    sent = (await send(bob, room_id, "original text", cmid)).json()
    await edit(bob, room_id, sent["id"], "edited text", 1)
    retry = await send(bob, room_id, "original text", cmid)  # the client retries its ORIGINAL send
    assert retry.status_code == 200
    assert (retry.json()["id"], retry.json()["body"], retry.json()["version"]) == (
        sent["id"],
        "edited text",
        2,
    )


# ---- deletion: permissions ----------------------------------------------------------------------------


async def test_owner_deletes_anyones_message_and_the_row_becomes_a_tombstone(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "SECRET-TEXT-TO-REMOVE")).json()
    total_before = await scalar(sessionmaker, "SELECT count(*) FROM messages")
    response = await delete(alice, room_id, sent["id"], 1)
    assert response.status_code == 204 and response.content == b""
    stored = await row(sessionmaker, uuid.UUID(sent["id"]))
    assert stored["body"] is None  # the text is gone from the database
    assert (stored["deleted_by"], stored["version"]) == (alice.id, 2)
    assert stored["deleted_at"] is not None and stored["author_id"] == bob.id
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM messages") == total_before
    )  # never removed
    shown = (await alice.get(f"/rooms/{room_id}/messages/{sent['id']}")).json()
    assert (shown["body"], shown["is_deleted"], shown["deleted_by"]) == (None, True, str(alice.id))
    history = (await carol.get(f"/rooms/{room_id}/messages")).text
    assert "SECRET-TEXT-TO-REMOVE" not in history  # nowhere in any response


async def test_an_editor_deletes_their_own_but_not_anothers(
    alice, bob, carol, make_actor, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    other = await make_actor("OtherEditor")
    await add_member(sessionmaker, room_id, other.id, "editor")
    mine = (await send(bob, room_id, "mine")).json()
    theirs = (await send(other, room_id, "theirs")).json()
    assert (await delete(bob, room_id, mine["id"], 1)).status_code == 204
    response = await delete(bob, room_id, theirs["id"], 1)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "message_not_author")
    assert (await row(sessionmaker, uuid.UUID(theirs["id"])))["body"] == "theirs"


async def test_a_viewer_can_delete_their_own_message_but_not_anothers(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)  # carol is a viewer
    mine = (await send(carol, room_id, "viewer's message")).json()
    theirs = (await send(bob, room_id, "the editor's message")).json()
    assert (await delete(carol, room_id, mine["id"], 1)).status_code == 204
    stored = await row(sessionmaker, uuid.UUID(mine["id"]))
    assert (stored["body"], stored["deleted_by"], stored["version"]) == (None, carol.id, 2)
    response = await delete(carol, room_id, theirs["id"], 1)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "message_not_author")
    assert (await row(sessionmaker, uuid.UUID(theirs["id"])))["body"] == "the editor's message"


async def test_the_owner_can_delete_a_viewers_message(alice, bob, carol, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(carol, room_id, "a viewer's words")).json()
    assert (await delete(alice, room_id, sent["id"], 1)).status_code == 204
    stored = await row(sessionmaker, uuid.UUID(sent["id"]))
    assert (stored["body"], stored["deleted_by"], stored["author_id"]) == (None, alice.id, carol.id)


async def test_a_downgraded_author_can_still_delete_their_own_message(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "written as editor")).json()
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    assert (await delete(bob, room_id, sent["id"], 1)).status_code == 204
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] is None


async def test_removed_and_outsider_users_cannot_delete(
    alice, bob, carol, make_actor, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "x")).json()
    outsider = await make_actor("Outsider")
    await add_member(sessionmaker, room_id, bob.id, "editor", "removed")
    for actor in (bob, outsider):
        response = await delete(actor, room_id, sent["id"], 1)
        assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] == "x"


async def test_delete_with_unknown_malformed_or_cross_room_ids(alice, sessionmaker):
    room_a, room_b = await new_room(alice, "A"), await new_room(alice, "B")
    in_b = (await send(alice, room_b, "in B")).json()
    for target in (str(uuid.uuid4()), "not-a-uuid", in_b["id"]):
        response = await delete(alice, room_a, target, 1)
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "message_not_found",
        )
    assert (await row(sessionmaker, uuid.UUID(in_b["id"])))["body"] == "in B"


# ---- deletion: versions, repeats, validation ---------------------------------------------------------------


async def test_stale_version_conflicts_when_deleting_a_live_message(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "v1")).json()
    await edit(bob, room_id, sent["id"], "v2", 1)
    response = await delete(alice, room_id, sent["id"], 1)  # the owner saw v1; it is now v2
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "message_version_conflict"
    assert response.json()["error"]["current_version"] == 2
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] == "v2"  # nothing deleted
    assert (await delete(alice, room_id, sent["id"], 2)).status_code == 204


async def test_deleting_an_already_deleted_message_is_204_even_with_a_stale_version(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "x")).json()
    await delete(alice, room_id, sent["id"], 1)
    before, buckets = (
        await row(sessionmaker, uuid.UUID(sent["id"])),
        await bucket_total(sessionmaker),
    )
    for version in (1, 2, 99):  # stale, current, absurd: all fine
        assert (await delete(alice, room_id, sent["id"], version)).status_code == 204
    assert (await delete(bob, room_id, sent["id"], 1)).status_code == 204  # the author, too
    assert await row(sessionmaker, uuid.UUID(sent["id"])) == before  # version, deleted_by unchanged
    assert await bucket_total(sessionmaker) == buckets  # no capacity used


async def test_authorization_comes_before_the_already_deleted_shortcut(
    alice, bob, carol, make_actor, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    other = await make_actor("OtherEditor")
    await add_member(sessionmaker, room_id, other.id, "editor")
    sent = (await send(bob, room_id, "x")).json()
    await delete(alice, room_id, sent["id"], 1)
    # a different editor learns nothing about a tombstone they may not touch
    response = await delete(other, room_id, sent["id"], 1)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "message_not_author")


async def test_retry_of_the_original_send_after_deletion_returns_the_tombstone(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    cmid = str(uuid.uuid4())
    sent = (await send(bob, room_id, "RESTORE-ME-IF-YOU-CAN", cmid)).json()
    await delete(alice, room_id, sent["id"], 1)
    retry = await send(bob, room_id, "RESTORE-ME-IF-YOU-CAN", cmid)
    assert retry.status_code == 200
    assert (retry.json()["id"], retry.json()["body"], retry.json()["is_deleted"]) == (
        sent["id"],
        None,
        True,
    )
    assert "RESTORE-ME" not in retry.text
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] is None  # still deleted
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == 1  # no new row either


@pytest.mark.parametrize("version", ["0", "-1", "1.5", "abc", "true", "", str(2**63)])
async def test_invalid_expected_version_on_delete_is_422(alice, sessionmaker, version):
    room_id = await new_room(alice)
    sent = (await send(alice, room_id, "x")).json()
    response = await alice.delete(
        f"/rooms/{room_id}/messages/{sent['id']}", params={"expected_version": version}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] == "x"


async def test_expected_version_is_required_on_delete(
    alice, bob, carol, make_actor, client, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "x")).json()
    path = f"/rooms/{room_id}/messages/{sent['id']}"
    assert (await alice.delete(path)).json()["error"]["code"] == "validation_error"
    # ...but it is validated AFTER the gates: others get their own error first
    outsider = await make_actor("Outsider")
    assert (await client.delete(path)).status_code == 401
    assert (await alice.delete(path, csrf=False)).json()["error"]["code"] == "csrf_failed"
    assert (await outsider.delete(path)).json()["error"]["code"] == "room_not_found"
    # a viewer is an active member: they pass the gates and then see the missing parameter
    assert (await carol.delete(path)).json()["error"]["code"] == "validation_error"


# ---- concurrent edits and deletes ---------------------------------------------------------------------------


async def test_two_concurrent_edits_with_the_same_version_give_one_winner(
    alice, bob, carol, sessionmaker
):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "v1")).json()
    results = await contended(
        sessionmaker,
        room_id,
        edit(bob, room_id, sent["id"], "edit from tab A", 1),
        edit(bob, room_id, sent["id"], "edit from tab B", 1),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"] == {
        "code": "message_version_conflict",
        "message": "The message changed since you loaded it.",
        "request_id": loser.headers["x-request-id"],
        "current_version": 2,
    }
    winner_body = next(r for r in results if r.status_code == 200).json()["body"]
    stored = await row(sessionmaker, uuid.UUID(sent["id"]))
    assert (stored["body"], stored["version"]) == (winner_body, 2)  # no silent last-write-wins


async def test_edit_versus_delete_leaves_a_consistent_message(alice, bob, carol, sessionmaker):
    room_id = await setup_room(alice, bob, carol, sessionmaker)
    sent = (await send(bob, room_id, "v1")).json()
    edit_response, delete_response = await contended(
        sessionmaker,
        room_id,
        edit(bob, room_id, sent["id"], "edited", 1),
        delete(alice, room_id, sent["id"], 1),
    )
    stored = await row(sessionmaker, uuid.UUID(sent["id"]))
    if edit_response.status_code == 200:  # edit first: the delete saw a stale version
        assert delete_response.json()["error"]["code"] == "message_version_conflict"
        assert (stored["body"], stored["version"], stored["deleted_at"]) == ("edited", 2, None)
    else:  # delete first: the message is a tombstone and cannot be edited
        assert (delete_response.status_code, edit_response.json()["error"]["code"]) == (
            204,
            "message_deleted",
        )
        assert (stored["body"], stored["version"]) == (None, 2)
    await execute(sessionmaker, "SELECT 1")
