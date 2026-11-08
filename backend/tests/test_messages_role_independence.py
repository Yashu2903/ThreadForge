"""Chat depends only on ACTIVE MEMBERSHIP, never on role.

Owner, editor and viewer can all read, send, edit their own and delete their own messages; only
the owner may delete others'. A role change never changes what a user can do with messages;
removal does. Role restrictions apply only to documents and uploads in later parts.
"""

import uuid
from pathlib import Path

import pytest

from tests.helpers import add_member
from tests.test_invitations_api import new_room
from tests.test_members_api import member_path
from tests.test_messages_edit_delete import delete, edit, row
from tests.test_messages_send import send

ROLES = ("owner", "editor", "viewer")


async def member_with_role(alice, make_actor, sessionmaker, room_id, role, name):
    """alice is the owner; for any other role create a member with that role."""
    if role == "owner":
        return alice
    actor = await make_actor(name)
    await add_member(sessionmaker, room_id, actor.id, role)
    return actor


@pytest.mark.parametrize("role", ROLES)
async def test_every_role_can_read_send_edit_own_and_delete_own(
    alice, make_actor, sessionmaker, role
):
    room_id = await new_room(alice)
    actor = await member_with_role(alice, make_actor, sessionmaker, room_id, role, f"The{role}")
    cmid = str(uuid.uuid4())

    sent = await send(actor, room_id, "first", cmid)
    assert sent.status_code == 201
    assert (await send(actor, room_id, "first", cmid)).status_code == 200  # matching retry
    assert (await actor.get(f"/rooms/{room_id}/messages")).status_code == 200  # read
    edited = await edit(actor, room_id, sent.json()["id"], "second", 1)  # edit own
    assert (edited.status_code, edited.json()["version"]) == (200, 2)
    assert (await delete(actor, room_id, sent.json()["id"], 2)).status_code == 204  # delete own
    tombstone = await send(actor, room_id, "first", cmid)  # a retry shows the tombstone
    assert (tombstone.status_code, tombstone.json()["body"]) == (200, None)


@pytest.mark.parametrize(("start_role", "new_role"), [("editor", "viewer"), ("viewer", "editor")])
async def test_a_role_change_never_changes_what_a_user_can_do_with_messages(
    alice, bob, sessionmaker, start_role, new_role
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, start_role)
    cmid = str(uuid.uuid4())
    earlier = (await send(bob, room_id, "written before the change", cmid)).json()
    to_delete = (await send(bob, room_id, "will be deleted", str(uuid.uuid4()))).json()

    # The owner changes the role through the real endpoint.
    changed = await alice.patch(member_path(room_id, bob), json={"role": new_role})
    assert (changed.status_code, changed.json()["role"]) == (200, new_role)

    after = await send(bob, room_id, "written after the change")  # send
    assert after.status_code == 201
    edited = await edit(bob, room_id, earlier["id"], "edited after the change", 1)  # edit earlier
    assert (edited.status_code, edited.json()["version"]) == (200, 2)
    retry = await send(bob, room_id, "written before the change", cmid)  # retry earlier send
    assert retry.status_code == 200 and retry.json()["id"] == earlier["id"]
    assert retry.json()["body"] == "edited after the change"
    assert (await delete(bob, room_id, to_delete["id"], 1)).status_code == 204  # delete own
    assert (await bob.get(f"/rooms/{room_id}/messages")).status_code == 200  # read
    # ...and the same user is still not the owner: others' messages stay off limits
    others = (await send(alice, room_id, "the owner's message")).json()
    refused = await delete(bob, room_id, others["id"], 1)
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "message_not_author")
    assert (await edit(bob, room_id, others["id"], "mine now", 1)).json()["error"][
        "code"
    ] == "message_not_author"


@pytest.mark.parametrize("role", ["editor", "viewer"])
async def test_removal_blocks_everything_whatever_the_role(alice, bob, sessionmaker, role):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, role)
    cmid = str(uuid.uuid4())
    sent = (await send(bob, room_id, "mine", cmid)).json()
    assert (await alice.delete(member_path(room_id, bob))).status_code == 204
    attempts = [
        send(bob, room_id, "mine", cmid),  # even a retry of an earlier send
        send(bob, room_id, "new"),
        edit(bob, room_id, sent["id"], "edit", 1),
        delete(bob, room_id, sent["id"], 1),
        bob.get(f"/rooms/{room_id}/messages"),
        bob.get(f"/rooms/{room_id}/messages/{sent['id']}"),
    ]
    for call in attempts:
        response = await call
        assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
        assert sent["id"] not in response.text and "mine" not in response.text  # no message data
    assert (await row(sessionmaker, uuid.UUID(sent["id"])))["body"] == "mine"  # untouched


async def test_no_message_route_ever_returns_insufficient_role(
    alice, bob, carol, make_actor, client, sessionmaker
):
    """Drive every message route as every kind of caller (all roles, removed, outsider, anonymous)
    and record every error code. insufficient_role (a Part 2 code for owner-only routes) must
    never be among them."""
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await add_member(sessionmaker, room_id, carol.id, "viewer")
    removed = await make_actor("Removed")
    await add_member(sessionmaker, room_id, removed.id, "viewer", "removed")
    outsider = await make_actor("Outsider")
    theirs = (await send(bob, room_id, "bob's")).json()

    codes: set[str] = set()
    for actor in (alice, bob, carol, removed, outsider):
        cmid = str(uuid.uuid4())
        mine = await send(actor, room_id, "probe", cmid)
        for response in [
            mine,
            await send(actor, room_id, "different", cmid),  # message_id_reused for members
            await send(actor, room_id, ""),  # validation
            await actor.get(f"/rooms/{room_id}/messages"),
            await actor.get(f"/rooms/{room_id}/messages/{theirs['id']}"),
            await edit(actor, room_id, theirs["id"], "x", 1),
            await edit(actor, room_id, str(uuid.uuid4()), "x", 1),
            await delete(actor, room_id, theirs["id"], 99),
            await delete(actor, room_id, str(uuid.uuid4()), 1),
            await actor.delete(f"/rooms/{room_id}/messages/{theirs['id']}"),  # missing version
        ]:
            if response.status_code >= 400:
                codes.add(response.json()["error"]["code"])
    anonymous = await client.post(
        f"/rooms/{room_id}/messages", json={"client_message_id": str(uuid.uuid4()), "body": "x"}
    )
    codes.add(anonymous.json()["error"]["code"])
    assert "insufficient_role" not in codes
    assert {"room_not_found", "message_not_author", "message_not_found", "unauthenticated"} <= codes


def test_the_chat_code_path_has_no_role_gate_left():
    """Structural guard: the only role comparison left in chat is the owner's delete privilege."""
    service = Path("app/services/messages.py").read_text(encoding="utf-8")
    routes = Path("app/api/messages.py").read_text(encoding="utf-8")
    assert "insufficient_role" not in service  # neither imported nor raised
    assert "insufficient_role" not in routes
    assert "WRITER_ROLES" not in service and "WriterAccess" not in routes
    assert service.count('role != "owner"') == 1  # the owner may delete anyone's message
