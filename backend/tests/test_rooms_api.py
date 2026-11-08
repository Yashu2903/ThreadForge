"""Room creation, listing, details, rename and members listing: permissions and ordering."""

import base64

import pytest

from app.services import rooms as rooms_service
from tests.factories import make_settings
from tests.helpers import add_member, execute, room_row, rows, scalar

ROOM_FIELDS = {
    "id", "name", "owner_id", "role", "membership_epoch", "created_at", "updated_at",
    "active_document_id", "active_document_version",
}  # fmt: skip


async def create(actor, name="Planning", **kw):
    response = await actor.post("/rooms", json={"name": name}, **kw)
    assert response.status_code == 201, response.text
    return response.json()


# ---- POST /rooms --------------------------------------------------------------------------------


async def test_create_room_makes_the_owner_membership_at_epoch_one(alice, sessionmaker):
    body = await create(alice, "  Team Room  ")
    assert set(body) == ROOM_FIELDS
    assert body["name"] == "Team Room"  # trimmed
    assert body["role"] == "owner" and body["owner_id"] == str(alice.id)
    assert body["membership_epoch"] == 1

    row = await room_row(sessionmaker, body["id"])
    assert row["membership_epoch"] == 1 and row["version"] == 0
    members = await rows(sessionmaker, "SELECT * FROM memberships")
    assert len(members) == 1
    assert (members[0]["user_id"], members[0]["role"], members[0]["state"]) == (
        alice.id,
        "owner",
        "active",
    )
    assert members[0]["removed_at"] is None


async def test_responses_are_never_cacheable(alice):
    response = await alice.post("/rooms", json={"name": "x"})
    assert response.headers["cache-control"] == "no-store"
    assert (await alice.get("/rooms")).headers["cache-control"] == "no-store"


async def test_unauthenticated_create_is_401_even_with_an_invalid_body(client):
    for payload in ({"name": "ok"}, {"name": ""}, {"junk": 1}):
        response = await client.post("/rooms", json=payload)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"


async def test_create_requires_csrf_after_authentication(alice, sessionmaker):
    for kwargs in ({"csrf": False}, {"origin": False}, {"csrf": False, "origin": False}):
        response = await alice.post("/rooms", json={"name": "x"}, **kwargs)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_failed"
    assert await scalar(sessionmaker, "SELECT count(*) FROM rooms") == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 121},
        {"name": 5},
        {"name": None},
        {"name": "bad\x00name"},
        {"name": "line\nbreak"},
        {"name": "ok", "owner_id": "00000000-0000-0000-0000-000000000001"},
        {"name": "ok", "role": "owner"},
        {"name": "ok", "membership_epoch": 99},
        {"name": "ok", "id": "00000000-0000-0000-0000-000000000001"},
    ],
)
async def test_invalid_or_unexpected_fields_are_rejected(alice, sessionmaker, payload):
    response = await alice.post("/rooms", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert await scalar(sessionmaker, "SELECT count(*) FROM rooms") == 0


async def test_name_length_boundaries_and_non_json_body(alice):
    assert (await alice.post("/rooms", json={"name": "x" * 120})).status_code == 201
    assert (await alice.post("/rooms", content=b"not json")).status_code == 422


async def test_failed_creation_leaves_no_partial_rows(alice, sessionmaker, monkeypatch):
    real_insert = rooms_service.insert

    def failing_insert(table):
        if table is rooms_service._MEMBERSHIPS:
            raise RuntimeError("simulated failure after the room row was inserted")
        return real_insert(table)

    monkeypatch.setattr(rooms_service, "insert", failing_insert)
    response = await alice.post("/rooms", json={"name": "doomed"})
    assert response.status_code == 500
    assert await scalar(sessionmaker, "SELECT count(*) FROM rooms") == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM memberships") == 0


async def test_owned_rooms_cap(make_actor, sessionmaker):
    capped = make_settings(max_owned_rooms_per_user=2)
    a = await make_actor("A", app_settings=capped)
    b = await make_actor("B", app_settings=capped)
    await create(a, "one")
    await create(a, "two")
    response = await a.post("/rooms", json={"name": "three"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "room_limit_reached"
    assert await scalar(sessionmaker, "SELECT count(*) FROM rooms WHERE owner_id = :u", u=a.id) == 2
    await create(b, "b's own cap is separate")


# ---- GET /rooms ---------------------------------------------------------------------------------


async def test_list_contains_only_my_active_rooms_newest_first(alice, bob, sessionmaker):
    first = await create(alice, "first")
    second = await create(alice, "second")
    bobs = await create(bob, "bob's")
    await add_member(sessionmaker, bobs["id"], alice.id, "viewer")  # alice is a viewer there
    removed = await create(bob, "alice was removed")
    await add_member(sessionmaker, removed["id"], alice.id, "editor", "removed")

    response = await alice.get("/rooms")
    assert response.status_code == 200
    body = response.json()
    names = [r["name"] for r in body["items"]]
    assert names == ["bob's", "second", "first"]  # newest first, removed room absent
    roles = {r["name"]: r["role"] for r in body["items"]}
    assert roles == {"bob's": "viewer", "second": "owner", "first": "owner"}
    assert body["next_cursor"] is None
    assert first["id"] != second["id"]


async def test_list_requires_authentication(client):
    assert (await client.get("/rooms")).status_code == 401


async def test_pagination_is_stable_when_timestamps_are_identical(alice, sessionmaker):
    for i in range(7):
        await create(alice, f"room {i}")
    await execute(sessionmaker, "UPDATE rooms SET created_at = '2026-01-01T00:00:00Z'")

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = (await alice.get("/rooms", params=params)).json()
        seen += [r["id"] for r in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and len(seen) == 7 and len(set(seen)) == 7  # no duplicates, none skipped
    expected = await rows(sessionmaker, "SELECT id FROM rooms ORDER BY created_at DESC, id DESC")
    assert seen == [str(r["id"]) for r in expected]


async def test_exact_page_boundary_has_no_phantom_next_page(alice):
    for i in range(4):
        await create(alice, f"r{i}")
    body = (await alice.get("/rooms", params={"limit": 4})).json()
    assert len(body["items"]) == 4 and body["next_cursor"] is None


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).rstrip(b"=").decode()


GOOD_UUID = "0000000a-0000-0000-0000-00000000000b"
GOOD_STAMP = "2026-01-01T00:00:00.000000+00:00"


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage!",
        "x",
        "A" * 300,
        _b64("not a cursor"),
        _b64(f"{GOOD_STAMP}|{GOOD_UUID.upper()}"),
        _b64(f"2026-01-01T00:00:00+00:00|{GOOD_UUID}"),  # not microsecond precision
        _b64(f"2026-01-01T00:00:00.000000+02:00|{GOOD_UUID}"),  # not UTC
        _b64(f"2026-01-01T00:00:00.000000|{GOOD_UUID}"),  # naive
        _b64(f"{GOOD_STAMP}|{GOOD_UUID}|extra"),
        _b64(f"{GOOD_STAMP}|{GOOD_UUID}") + "=",  # padded: not canonical
        _b64(f"{GOOD_STAMP}|{GOOD_UUID}") + "A",
    ],
)
async def test_invalid_cursors_are_rejected(alice, cursor):
    response = await alice.get("/rooms", params={"cursor": cursor})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", ""])
async def test_invalid_limits_are_validation_errors(alice, limit):
    response = await alice.get("/rooms", params={"limit": limit})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_limit_100_is_allowed(alice):
    assert (await alice.get("/rooms", params={"limit": 100})).status_code == 200


# ---- GET /rooms/{id} ----------------------------------------------------------------------------


async def test_member_reads_room_details_with_their_own_role(alice, bob, sessionmaker):
    room = await create(alice, "Details")
    await add_member(sessionmaker, room["id"], bob.id, "viewer")
    mine = (await alice.get(f"/rooms/{room['id']}")).json()
    theirs = (await bob.get(f"/rooms/{room['id']}")).json()
    assert set(mine) == ROOM_FIELDS == set(theirs)
    assert (mine["role"], theirs["role"]) == ("owner", "viewer")
    assert mine["name"] == theirs["name"] == "Details"


async def test_nonmember_and_unknown_room_are_indistinguishable_404s(alice, bob):
    room = await create(alice, "Secret name")
    nonmember = await bob.get(f"/rooms/{room['id']}")
    unknown = await bob.get("/rooms/00000000-0000-0000-0000-000000000009")
    malformed = await bob.get("/rooms/not-a-uuid")
    for response in (nonmember, unknown, malformed):
        assert response.status_code == 404
        error = response.json()["error"]
        assert (error["code"], error["message"]) == ("room_not_found", "Room not found.")
        assert "Secret name" not in response.text
    assert (await alice.get(f"/rooms/{room['id']}")).status_code == 200


async def test_room_read_requires_authentication(alice, client):
    room = await create(alice)
    assert (await client.get(f"/rooms/{room['id']}")).status_code == 401


async def test_removed_member_loses_http_access(alice, bob, sessionmaker):
    room = await create(alice)
    await add_member(sessionmaker, room["id"], bob.id, "editor")
    assert (await bob.get(f"/rooms/{room['id']}")).status_code == 200
    await add_member(sessionmaker, room["id"], bob.id, "editor", "removed")
    for path in (f"/rooms/{room['id']}", f"/rooms/{room['id']}/members"):
        assert (await bob.get(path)).status_code == 404


# ---- PATCH /rooms/{id} --------------------------------------------------------------------------


async def test_owner_renames_room_bumping_version_but_not_epoch(alice, sessionmaker):
    room = await create(alice, "Old")
    response = await alice.patch(f"/rooms/{room['id']}", json={"name": "  New  "})
    assert response.status_code == 200
    assert response.json()["name"] == "New"
    row = await room_row(sessionmaker, room["id"])
    assert (row["name"], row["version"], row["membership_epoch"]) == ("New", 1, 1)
    assert row["updated_at"] >= row["created_at"]


async def test_same_name_rename_is_a_no_op(alice, sessionmaker):
    room = await create(alice, "Same")
    assert (await alice.patch(f"/rooms/{room['id']}", json={"name": "Same"})).status_code == 200
    row = await room_row(sessionmaker, room["id"])
    assert (row["version"], row["membership_epoch"]) == (0, 1)


@pytest.mark.parametrize(
    ("role", "status", "code"),
    [("editor", 403, "insufficient_role"), ("viewer", 403, "insufficient_role")],
)
async def test_only_the_owner_can_rename(alice, bob, sessionmaker, role, status, code):
    room = await create(alice, "Keep")
    await add_member(sessionmaker, room["id"], bob.id, role)
    response = await bob.patch(f"/rooms/{room['id']}", json={"name": "Hijacked"})
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert (await room_row(sessionmaker, room["id"]))["name"] == "Keep"


async def test_nonmember_rename_is_404_even_with_an_invalid_body(alice, bob, sessionmaker):
    room = await create(alice, "Keep")
    for payload in ({"name": "ok"}, {"name": ""}, {"name": 5}, {"junk": 1}, {}):
        response = await bob.patch(f"/rooms/{room['id']}", json=payload)
        assert response.status_code == 404, payload
        assert response.json()["error"]["code"] == "room_not_found"


async def test_non_owner_member_with_invalid_body_gets_403_not_422(alice, bob, sessionmaker):
    room = await create(alice)
    await add_member(sessionmaker, room["id"], bob.id, "viewer")
    response = await bob.patch(f"/rooms/{room['id']}", json={"name": ""})
    assert response.status_code == 403


async def test_csrf_is_checked_before_room_authorization(alice, bob):
    room = await create(alice)
    # A nonmember WITHOUT a CSRF token is told csrf_failed (403), not room_not_found: CSRF comes
    # before room authorization in the documented order.
    response = await bob.patch(f"/rooms/{room['id']}", json={"name": "x"}, csrf=False)
    assert response.status_code == 403 and response.json()["error"]["code"] == "csrf_failed"
    # And unauthenticated beats everything.
    anonymous = alice.client.__class__(
        transport=alice.client._transport, base_url="http://localhost:8000"
    )
    async with anonymous:
        assert (
            await anonymous.patch(f"/rooms/{room['id']}", json={"name": "x"})
        ).status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "ok", "owner_id": "00000000-0000-0000-0000-000000000001"},
        {"name": "ok", "membership_epoch": 5},
        {"name": "ok", "version": 9},
        {"owner_id": "00000000-0000-0000-0000-000000000001"},
        {},
        {"name": ""},
    ],
)
async def test_rename_rejects_unexpected_or_invalid_fields(alice, sessionmaker, payload):
    room = await create(alice, "Keep")
    response = await alice.patch(f"/rooms/{room['id']}", json=payload)
    assert response.status_code == 422
    row = await room_row(sessionmaker, room["id"])
    assert (row["name"], row["owner_id"], row["membership_epoch"], row["version"]) == (
        "Keep",
        alice.id,
        1,
        0,
    )


async def test_rename_cannot_target_another_users_room_by_id_guessing(alice, bob):
    mine = await create(alice, "mine")
    theirs = await create(bob, "theirs")
    assert (await alice.patch(f"/rooms/{theirs['id']}", json={"name": "x"})).status_code == 404
    assert (await alice.get(f"/rooms/{mine['id']}")).json()["name"] == "mine"


# ---- GET /rooms/{id}/members ---------------------------------------------------------------------


async def test_members_list_has_no_email_or_provider_data(alice, bob, carol, sessionmaker):
    await execute(sessionmaker, "UPDATE users SET email = display_name || '@example.com'")
    room = await create(alice)
    await add_member(sessionmaker, room["id"], bob.id, "editor")
    await add_member(sessionmaker, room["id"], carol.id, "viewer", "removed")
    body = (await alice.get(f"/rooms/{room['id']}/members")).json()
    assert {m["display_name"] for m in body["items"]} == {"Alice", "Bob"}  # carol is removed
    for member in body["items"]:
        assert set(member) == {"user_id", "display_name", "role", "joined_at"}
    assert "@example.com" not in str(body)
    assert (await bob.get(f"/rooms/{room['id']}/members")).status_code == 200  # editors may list


async def test_nonmember_cannot_list_members(alice, bob):
    room = await create(alice)
    response = await bob.get(f"/rooms/{room['id']}/members")
    assert response.status_code == 404 and response.json()["error"]["code"] == "room_not_found"


async def test_member_pagination_with_identical_join_times(alice, make_actor, sessionmaker):
    room = await create(alice)
    for i in range(6):
        extra = await make_actor(f"Extra{i}")
        await add_member(sessionmaker, room["id"], extra.id, "viewer")
    await execute(sessionmaker, "UPDATE memberships SET joined_at = '2026-01-01T00:00:00Z'")

    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (await alice.get(f"/rooms/{room['id']}/members", params=params)).json()
        seen += [m["user_id"] for m in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    expected = await rows(
        sessionmaker, "SELECT user_id FROM memberships ORDER BY joined_at ASC, user_id ASC"
    )
    assert seen == [str(r["user_id"]) for r in expected] and len(set(seen)) == 7


async def test_members_invalid_cursor_after_authorization(alice, bob):
    room = await create(alice)
    # Nonmember with a bad cursor: 404 (authorization first), not invalid_cursor.
    assert (
        await bob.get(f"/rooms/{room['id']}/members", params={"cursor": "bad!"})
    ).status_code == 404
    response = await alice.get(f"/rooms/{room['id']}/members", params={"cursor": "bad!"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_cursor"


async def test_user_with_no_membership_row_lists_no_rooms(alice, bob, carol, sessionmaker):
    """carol has NO membership row anywhere, while rooms exist for other people."""
    await create(alice, "alice's")
    shared = await create(bob, "bob's")
    await add_member(sessionmaker, shared["id"], alice.id, "viewer")
    assert (
        await scalar(
            sessionmaker, "SELECT count(*) FROM memberships WHERE user_id = :u", u=carol.id
        )
        == 0
    )

    response = await carol.get("/rooms")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    # Nothing leaks by guessing ids either.
    for room in await rows(sessionmaker, "SELECT id FROM rooms"):
        assert (await carol.get(f"/rooms/{room['id']}")).status_code == 404


async def test_list_tracks_membership_rows_as_they_change(alice, bob, sessionmaker):
    room = await create(alice, "Toggle")
    assert (await bob.get("/rooms")).json()["items"] == []  # no row
    await add_member(sessionmaker, room["id"], bob.id, "viewer")
    assert [r["id"] for r in (await bob.get("/rooms")).json()["items"]] == [room["id"]]  # active
    await add_member(sessionmaker, room["id"], bob.id, "viewer", "removed")
    assert (await bob.get("/rooms")).json()["items"] == []  # removed row grants nothing
    await execute(sessionmaker, "DELETE FROM memberships WHERE user_id = :u", u=bob.id)
    assert (await bob.get("/rooms")).json()["items"] == []  # row gone entirely
