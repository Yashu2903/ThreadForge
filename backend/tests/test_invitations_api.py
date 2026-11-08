"""Invitation creation, listing, revocation and redemption (spec section 7, rule by rule)."""

import logging
import re

import pytest

from app.services.security import sha256_hex
from tests.factories import make_settings
from tests.helpers import add_member, execute, room_row, rows, scalar

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
INVALID = ("invitation_invalid", "This invitation is not valid.")


async def new_room(owner, name="Room"):
    response = await owner.post("/rooms", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


async def invite(owner, room_id, **body):
    response = await owner.post(f"/rooms/{room_id}/invitations", json=body or None)
    assert response.status_code == 201, response.text
    return response.json()


async def redeem(actor, token, **kw):
    return await actor.post("/invitations/redeem", json={"token": token}, **kw)


def assert_invalid(response):
    assert response.status_code == 404
    error = response.json()["error"]
    assert (error["code"], error["message"]) == INVALID
    return response


async def expire(sm, invitation_id):
    await execute(
        sm,
        "UPDATE invitations SET created_at = now() - interval '2 days', "
        "expires_at = now() - interval '1 hour' WHERE id = :i",
        i=invitation_id,
    )


async def inv_row(sm, invitation_id):
    [row] = await rows(sm, "SELECT * FROM invitations WHERE id = :i", i=invitation_id)
    return row


async def epoch(sm, room_id):
    return (await room_row(sm, room_id))["membership_epoch"]


# ---- creation -----------------------------------------------------------------------------------


async def test_create_returns_the_raw_token_once_and_stores_only_its_hash(alice, sessionmaker):
    room_id = await new_room(alice)
    response = await alice.post(f"/rooms/{room_id}/invitations", json={})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "room_id", "role", "expires_at", "token", "url"}
    assert body["role"] == "editor" and body["room_id"] == room_id  # default role
    assert TOKEN_RE.fullmatch(body["token"])
    assert body["url"] == f"http://localhost:8000/join#token={body['token']}"
    assert response.headers["cache-control"] == "no-store"

    row = await inv_row(sessionmaker, body["id"])
    assert row["token_hash"] == sha256_hex(body["token"]) != body["token"]
    dump = str(await rows(sessionmaker, "SELECT * FROM invitations"))
    assert body["token"] not in dump  # the raw token exists nowhere in the database

    listed = await alice.get(f"/rooms/{room_id}/invitations")
    assert body["token"] not in listed.text and row["token_hash"] not in listed.text
    assert "token" not in listed.json()["items"][0]


async def test_default_and_bounded_lifetimes(alice, sessionmaker):
    room_id = await new_room(alice)
    for hours, expected in ((None, 24), (1, 1), (168, 168)):
        body = await invite(
            alice, room_id, **({} if hours is None else {"expires_in_hours": hours})
        )
        seconds = await scalar(
            sessionmaker,
            "SELECT extract(epoch FROM expires_at - created_at) FROM invitations WHERE id = :i",
            i=body["id"],
        )
        assert seconds == expected * 3600


async def test_creating_an_invitation_does_not_change_the_epoch_or_version(alice, sessionmaker):
    room_id = await new_room(alice)
    await invite(alice, room_id)
    row = await room_row(sessionmaker, room_id)
    assert (row["membership_epoch"], row["version"]) == (1, 0)


@pytest.mark.parametrize(
    "payload",
    [
        {"expires_in_hours": 0},
        {"expires_in_hours": 169},
        {"expires_in_hours": -5},
        {"expires_in_hours": "5"},
        {"expires_in_hours": 5.5},
        {"expires_in_hours": True},
        {"role": "owner"},
        {"role": "admin"},
        {"role": ""},
        {"role": None},
        {"room_id": "00000000-0000-0000-0000-000000000001"},
        {"created_by": "00000000-0000-0000-0000-000000000001"},
        {"token": "A" * 43},
        {"expires_at": "2099-01-01T00:00:00Z"},
    ],
)
async def test_invalid_creation_bodies_are_rejected(alice, sessionmaker, payload):
    room_id = await new_room(alice)
    response = await alice.post(f"/rooms/{room_id}/invitations", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert await scalar(sessionmaker, "SELECT count(*) FROM invitations") == 0


async def test_creation_permission_matrix_and_check_order(alice, bob, carol, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await add_member(sessionmaker, room_id, carol.id, "viewer")
    path = f"/rooms/{room_id}/invitations"
    for member in (bob, carol):
        response = await member.post(path, json={})
        assert (response.status_code, response.json()["error"]["code"]) == (
            403,
            "insufficient_role",
        )
        # a non-owner MEMBER with an invalid body is still 403, not 422
        assert (await member.post(path, json={"role": "owner"})).status_code == 403
        assert (await member.post(path, json={}, csrf=False)).json()["error"][
            "code"
        ] == "csrf_failed"
    assert await scalar(sessionmaker, "SELECT count(*) FROM invitations") == 0


async def test_nonmember_gets_404_even_with_an_invalid_body(alice, bob):
    room_id = await new_room(alice)
    for payload in ({}, {"role": "owner"}, {"expires_in_hours": 0}, {"junk": 1}):
        response = await bob.post(f"/rooms/{room_id}/invitations", json=payload)
        assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")


async def test_creation_requires_csrf_and_authentication(alice, client):
    room_id = await new_room(alice)
    path = f"/rooms/{room_id}/invitations"
    assert (await alice.post(path, json={}, csrf=False)).status_code == 403
    assert (await alice.post(path, json={}, origin=False)).status_code == 403
    assert (await client.post(path, json={})).status_code == 401


async def test_active_invitation_cap_counts_only_active_ones(make_actor, sessionmaker):
    capped = make_settings(max_active_invitations_per_room=2)
    owner = await make_actor("Owner", app_settings=capped)
    room_id = await new_room(owner)
    first = await invite(owner, room_id)
    second = await invite(owner, room_id)
    response = await owner.post(f"/rooms/{room_id}/invitations", json={})
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "invitation_limit_reached",
    )

    await expire(sessionmaker, first["id"])  # expired no longer counts
    third = await invite(owner, room_id)
    assert (await owner.delete(f"/rooms/{room_id}/invitations/{second['id']}")).status_code == 204
    await invite(owner, room_id)  # revoked no longer counts
    assert (await owner.post(f"/rooms/{room_id}/invitations", json={})).status_code == 409
    assert third["id"] != first["id"]


# ---- listing ------------------------------------------------------------------------------------


async def test_list_shows_metadata_and_computed_status(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    active = await invite(alice, room_id)
    used = await invite(alice, room_id)
    revoked = await invite(alice, room_id)
    expired = await invite(alice, room_id)
    assert (await redeem(bob, used["token"])).status_code == 200
    await alice.delete(f"/rooms/{room_id}/invitations/{revoked['id']}")
    await expire(sessionmaker, expired["id"])

    items = (await alice.get(f"/rooms/{room_id}/invitations")).json()["items"]
    status = {i["id"]: i["status"] for i in items}
    assert status == {
        active["id"]: "active",
        used["id"]: "used",
        revoked["id"]: "revoked",
        expired["id"]: "expired",
    }
    one = next(i for i in items if i["id"] == used["id"])
    assert set(one) == {
        "id",
        "role",
        "created_by",
        "created_at",
        "expires_at",
        "used_at",
        "used_by",
        "revoked_at",
        "status",
    }
    assert one["used_by"] == str(bob.id) and one["created_by"] == str(alice.id)


async def test_used_beats_revoked_and_expired_in_status_precedence(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    await redeem(bob, inv["token"])
    await expire(sessionmaker, inv["id"])  # also expired now
    items = (await alice.get(f"/rooms/{room_id}/invitations")).json()["items"]
    assert items[0]["status"] == "used"


async def test_list_permissions(alice, bob, carol, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await add_member(sessionmaker, room_id, carol.id, "viewer")
    path = f"/rooms/{room_id}/invitations"
    assert (await alice.get(path)).status_code == 200
    for member in (bob, carol):
        assert (await member.get(path)).json()["error"]["code"] == "insufficient_role"
    other = await new_room(bob, "bob's room")
    response = await alice.get(f"/rooms/{other}/invitations")
    assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")


async def test_list_is_newest_first_and_paginates_stably(alice, sessionmaker):
    room_id = await new_room(alice)
    for _ in range(5):
        await invite(alice, room_id)
    await execute(sessionmaker, "UPDATE invitations SET created_at = '2026-01-01T00:00:00Z'")
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (await alice.get(f"/rooms/{room_id}/invitations", params=params)).json()
        seen += [i["id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    expected = await rows(
        sessionmaker, "SELECT id FROM invitations ORDER BY created_at DESC, id DESC"
    )
    assert seen == [str(r["id"]) for r in expected] and len(set(seen)) == 5
    bad = await alice.get(f"/rooms/{room_id}/invitations", params={"cursor": "nope!"})
    assert bad.json()["error"]["code"] == "invalid_cursor"


# ---- revocation ---------------------------------------------------------------------------------


async def test_revoke_sets_revoked_at_keeps_the_row_and_is_idempotent(alice, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    path = f"/rooms/{room_id}/invitations/{inv['id']}"
    assert (await alice.delete(path)).status_code == 204
    first = (await inv_row(sessionmaker, inv["id"]))["revoked_at"]
    assert first is not None
    assert (await alice.delete(path)).status_code == 204  # repeated: still 204
    assert (await inv_row(sessionmaker, inv["id"]))["revoked_at"] == first  # unchanged
    assert await epoch(sessionmaker, room_id) == 1  # revocation never touches the epoch


async def test_expired_unused_invitation_can_still_be_revoked(alice, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    await expire(sessionmaker, inv["id"])
    assert (await alice.delete(f"/rooms/{room_id}/invitations/{inv['id']}")).status_code == 204


async def test_used_invitation_cannot_be_revoked(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    await redeem(bob, inv["token"])
    response = await alice.delete(f"/rooms/{room_id}/invitations/{inv['id']}")
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "invitation_already_redeemed",
    )
    assert (await inv_row(sessionmaker, inv["id"]))["revoked_at"] is None


async def test_invitation_from_another_room_cannot_be_touched(alice, bob, sessionmaker):
    mine = await new_room(alice, "mine")
    theirs = await new_room(bob, "theirs")
    their_inv = await invite(bob, theirs)
    # Alice owns `mine` and passes a real invitation id that belongs to ANOTHER room.
    response = await alice.delete(f"/rooms/{mine}/invitations/{their_inv['id']}")
    assert (response.status_code, response.json()["error"]["code"]) == (404, "invitation_not_found")
    assert (await inv_row(sessionmaker, their_inv["id"]))["revoked_at"] is None
    for bad_id in ("not-a-uuid", "00000000-0000-0000-0000-000000000009"):
        response = await alice.delete(f"/rooms/{mine}/invitations/{bad_id}")
        assert response.json()["error"]["code"] == "invitation_not_found"


async def test_revoke_permissions_and_csrf(alice, bob, carol, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    path = f"/rooms/{room_id}/invitations/{inv['id']}"
    assert (await bob.delete(path)).json()["error"]["code"] == "insufficient_role"
    assert (await carol.delete(path)).json()["error"]["code"] == "room_not_found"
    assert (await alice.delete(path, csrf=False)).json()["error"]["code"] == "csrf_failed"
    assert (await inv_row(sessionmaker, inv["id"]))["revoked_at"] is None


async def test_revoked_invitation_cannot_be_redeemed(alice, bob):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    await alice.delete(f"/rooms/{room_id}/invitations/{inv['id']}")
    assert_invalid(await redeem(bob, inv["token"]))
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 404


# ---- redemption ---------------------------------------------------------------------------------


async def test_redeem_creates_the_membership_consumes_the_invitation_and_bumps_the_epoch(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice, "Shared")
    inv = await invite(alice, room_id, role="viewer")
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 404  # before: no access

    response = await redeem(bob, inv["token"])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"room", "membership"}
    assert body["room"]["id"] == room_id and body["room"]["role"] == "viewer"
    assert body["room"]["membership_epoch"] == 2
    assert body["membership"]["user_id"] == str(bob.id) and body["membership"]["role"] == "viewer"

    row = await inv_row(sessionmaker, inv["id"])
    assert row["used_by"] == bob.id and row["used_at"] is not None
    assert await epoch(sessionmaker, room_id) == 2
    assert (await bob.get(f"/rooms/{room_id}")).json()["role"] == "viewer"
    members = (await bob.get(f"/rooms/{room_id}/members")).json()["items"]
    assert {m["display_name"] for m in members} == {"Alice", "Bob"}


async def test_redeem_requires_authentication_and_csrf(alice, bob, client, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    assert (
        await client.post("/invitations/redeem", json={"token": inv["token"]})
    ).status_code == 401
    assert (await redeem(bob, inv["token"], csrf=False)).json()["error"]["code"] == "csrf_failed"
    assert (await redeem(bob, inv["token"], origin=False)).status_code == 403
    assert (await inv_row(sessionmaker, inv["id"]))["used_at"] is None
    assert (await redeem(bob, inv["token"])).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": 5},
        {"token": None},
        {"token": ["a"]},
        {"token": "x" * 43, "room_id": "r"},
        {"role": "owner"},
    ],
)
async def test_redeem_rejects_structurally_invalid_bodies(bob, payload):
    response = await bob.post("/invitations/redeem", json=payload)
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error"


async def test_malformed_unknown_expired_revoked_and_used_tokens_are_indistinguishable(
    alice, bob, carol, sessionmaker
):
    room_id = await new_room(alice)
    expired = await invite(alice, room_id)
    revoked = await invite(alice, room_id)
    used = await invite(alice, room_id)
    await expire(sessionmaker, expired["id"])
    await alice.delete(f"/rooms/{room_id}/invitations/{revoked['id']}")
    assert (await redeem(carol, used["token"])).status_code == 200

    tokens = [
        "",
        "short",
        "A" * 42,
        "A" * 44,
        "!" * 43,
        "é" * 43,
        "A" * 10_000,
        "A" * 43,  # well-formed but unknown
        expired["token"],
        revoked["token"],
        used["token"],  # used by CAROL, so invalid for bob
    ]
    bodies = set()
    for token in tokens:
        response = assert_invalid(await redeem(bob, token))
        bodies.add(
            (
                response.status_code,
                response.json()["error"]["code"],
                response.json()["error"]["message"],
            )
        )
    assert len(bodies) == 1  # one status, one code, one message for every reason
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 404


async def test_same_user_retry_returns_the_same_membership_without_changes(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    first = await redeem(bob, inv["token"])
    before = await rows(sessionmaker, "SELECT * FROM memberships WHERE user_id = :u", u=bob.id)
    retry = await redeem(bob, inv["token"])
    assert retry.status_code == 200
    assert retry.json()["membership"] == first.json()["membership"]
    assert retry.json()["room"]["membership_epoch"] == 2
    assert (
        await rows(sessionmaker, "SELECT * FROM memberships WHERE user_id = :u", u=bob.id) == before
    )
    assert await epoch(sessionmaker, room_id) == 2


async def test_same_user_retry_still_works_after_the_invitation_expired(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    await redeem(bob, inv["token"])
    await expire(sessionmaker, inv["id"])
    assert (await redeem(bob, inv["token"])).status_code == 200


async def test_removed_user_cannot_reuse_their_used_invitation(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    await redeem(bob, inv["token"])
    await add_member(sessionmaker, room_id, bob.id, "editor", "removed")
    assert_invalid(await redeem(bob, inv["token"]))
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 404


async def test_existing_member_redeeming_does_not_consume_or_change_anything(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    editor_invite = await invite(alice, room_id, role="editor")
    before_epoch = await epoch(sessionmaker, room_id)

    response = await redeem(bob, editor_invite["token"])
    assert response.status_code == 200
    assert response.json()["membership"]["role"] == "viewer"  # role NOT upgraded
    assert (await inv_row(sessionmaker, editor_invite["id"]))["used_at"] is None  # not consumed
    assert await epoch(sessionmaker, room_id) == before_epoch

    owner_response = await redeem(alice, editor_invite["token"])  # even the owner
    assert owner_response.status_code == 200
    assert owner_response.json()["membership"]["role"] == "owner"
    assert (await inv_row(sessionmaker, editor_invite["id"]))["used_at"] is None


async def test_new_invitation_reactivates_a_removed_membership_and_resets_joined_at(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    first = await invite(alice, room_id, role="editor")
    await redeem(bob, first["token"])
    await add_member(sessionmaker, room_id, bob.id, "editor", "removed")
    await execute(
        sessionmaker,
        "UPDATE memberships SET joined_at = '2026-01-01T00:00:00Z' WHERE user_id = :u",
        u=bob.id,
    )
    epoch_before = await epoch(sessionmaker, room_id)

    fresh = await invite(alice, room_id, role="viewer")
    response = await redeem(bob, fresh["token"])
    assert response.status_code == 200
    assert response.json()["membership"]["role"] == "viewer"  # the NEW invitation's role

    [row] = await rows(sessionmaker, "SELECT * FROM memberships WHERE user_id = :u", u=bob.id)
    assert (row["state"], row["role"], row["removed_at"]) == ("active", "viewer", None)
    assert row["joined_at"].year >= 2026 and row["joined_at"].isoformat() > "2026-01-02"  # reset
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM memberships WHERE user_id = :u", u=bob.id)
        == 1
    )
    assert await epoch(sessionmaker, room_id) == epoch_before + 1
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 200


async def test_member_cap_blocks_redemption_without_consuming_the_invitation(
    make_actor, sessionmaker
):
    capped = make_settings(max_members_per_room=2)
    owner = await make_actor("Owner", app_settings=capped)
    first = await make_actor("First", app_settings=capped)
    second = await make_actor("Second", app_settings=capped)
    room_id = await new_room(owner)
    assert (await redeem(first, (await invite(owner, room_id))["token"])).status_code == 200
    blocked = await invite(owner, room_id)
    epoch_before = await epoch(sessionmaker, room_id)

    response = await redeem(second, blocked["token"])
    assert (response.status_code, response.json()["error"]["code"]) == (409, "member_limit_reached")
    assert (await inv_row(sessionmaker, blocked["id"]))["used_at"] is None  # untouched
    assert (
        await scalar(
            sessionmaker, "SELECT count(*) FROM memberships WHERE user_id = :u", u=second.id
        )
        == 0
    )
    assert await epoch(sessionmaker, room_id) == epoch_before

    # An existing member retrying is still fine even when the room is full (no new seat needed).
    assert (await redeem(first, blocked["token"])).status_code == 200


async def test_redemption_cannot_grant_ownership_or_choose_a_room(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id, role="editor")
    other_room = await new_room(alice, "other")
    for extra in ({"role": "owner"}, {"room_id": other_room}):
        response = await bob.post("/invitations/redeem", json={"token": inv["token"], **extra})
        assert response.status_code == 422
    assert (await redeem(bob, inv["token"])).json()["room"]["id"] == room_id
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM memberships WHERE role = 'owner'") == 2
    )  # one per room
    assert (await bob.get(f"/rooms/{other_room}")).status_code == 404


async def test_token_and_hash_never_appear_in_logs(alice, bob, caplog):
    caplog.set_level(logging.DEBUG)
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    await redeem(bob, inv["token"])
    await redeem(bob, "A" * 43)
    logged = "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records)
    assert "action=invitation_created" in logged and "action=member_joined" in logged
    assert inv["token"] not in logged and sha256_hex(inv["token"]) not in logged


async def test_a_join_link_get_never_joins_a_room(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    inv = await invite(alice, room_id)
    for path in ("/join", "/invitations/redeem", f"/join?token={inv['token']}"):
        assert (await bob.get(path)).status_code in (404, 405)
    assert (await bob.get(f"/rooms/{room_id}")).status_code == 404
    assert (await inv_row(sessionmaker, inv["id"]))["used_at"] is None
