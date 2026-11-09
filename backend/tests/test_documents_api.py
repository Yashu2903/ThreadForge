"""Document creation, listing, reading, renaming and the active-document selection."""

import asyncio
import uuid

import pytest

from app.services import documents as documents_service
from tests.doc_helpers import create_doc, events_of, new_id, room_with_doc
from tests.factories import make_settings
from tests.helpers import add_member, execute, room_row, rows, scalar
from tests.test_invitations_api import new_room

SUMMARY_FIELDS = {
    "id", "room_id", "title", "template_key", "schema_version", "metadata_version", "created_by",
    "created_at", "updated_at", "is_active", "requires_review", "collab_state",
}  # fmt: skip
# the single-document answer adds how to OPEN it; it never carries the text (the collaboration
# service delivers that)
DOC_FIELDS = SUMMARY_FIELDS | {"can_edit", "collab_path", "document_name"}


async def roles_room(alice, bob, carol, make_actor, sessionmaker):
    """alice owner, bob editor, carol viewer, dave REMOVED, erin a stranger; one active document."""
    dave, erin = await make_actor("Dave"), await make_actor("Erin")
    room_id, doc = await room_with_doc(
        alice, sessionmaker, (bob, "editor"), (carol, "viewer"), (dave, "editor")
    )
    await execute(
        sessionmaker,
        "UPDATE memberships SET state = 'removed', removed_at = now() WHERE room_id = :r AND user_id = :u",
        r=room_id,
        u=dave.id,
    )
    return (
        room_id,
        doc,
        {"owner": alice, "editor": bob, "viewer": carol, "removed": dave, "stranger": erin},
    )


# ---- creation ---------------------------------------------------------------------------------------


async def test_owner_creates_a_document_that_becomes_active_with_two_ordered_events(
    alice, sessionmaker
):
    room_id = await new_room(alice)
    response = await create_doc(alice, room_id, "  Launch plan  ", "project_brief")
    assert response.status_code == 201
    body = response.json()
    assert set(body) == DOC_FIELDS
    assert (body["title"], body["template_key"], body["metadata_version"]) == (
        "Launch plan", "project_brief", 1,
    )  # fmt: skip
    assert (body["is_active"], body["can_edit"], body["requires_review"]) == (True, True, False)
    assert body["collab_state"] == "legacy"  # seeded into Yjs on first open, not at creation
    assert (body["collab_path"], body["document_name"]) == ("/collab", f"doc:{body['id']}")
    assert "content" not in body and "content_hash" not in body and "content_version" not in body
    room = await room_row(sessionmaker, room_id)
    assert (str(room["active_document_id"]), room["active_document_version"]) == (body["id"], 1)
    assert room["version"] == 0  # the selection is not a room rename or access change

    events = await events_of(sessionmaker, room_id, after=1)  # seq 1 is room_created
    assert [e["type"] for e in events] == ["document_created", "active_document_changed"]
    assert [e["seq"] for e in events] == [2, 3]
    assert events[0]["payload"] == {"document_id": body["id"]}
    assert events[1]["payload"] == {"document_id": body["id"], "selection_version": 1}
    assert {e["resource_type"] for e in events} == {"document"}
    assert room["event_seq"] == 3
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE state = 'pending'") == 3
    # events carry references, never titles or content
    assert "Launch" not in str(events)


async def test_a_second_document_replaces_the_first_as_active_and_bumps_the_version(
    alice, sessionmaker
):
    room_id, first = await room_with_doc(alice, sessionmaker)
    second = (await create_doc(alice, room_id, "Second")).json()
    room = await room_row(sessionmaker, room_id)
    assert str(room["active_document_id"]) == second["id"] and room["active_document_version"] == 2
    listing = (await alice.get(f"/rooms/{room_id}/documents")).json()["items"]
    assert [(d["id"], d["is_active"]) for d in listing] == [
        (second["id"], True),
        (first["id"], False),
    ]


async def test_every_template_is_created_valid_with_unique_ids(alice, sessionmaker):
    room_id = await new_room(alice)
    seen = set()
    for key in (
        "blank",
        "project_brief",
        "technical_proposal",
        "meeting_notes",
        "research_summary",
    ):
        body = (await create_doc(alice, room_id, key, key)).json()
        assert "content" not in body  # the API never returns text; the seed is stored read-only
        [seed] = await rows(
            sessionmaker, "SELECT content FROM documents WHERE id = :d", d=body["id"]
        )
        ids = [b["id"] for b in seed["content"]["blocks"]]
        assert len(set(ids)) == len(ids) and not (set(ids) & seen)
        seen |= set(ids)
        assert seed["content"]["schema_version"] == 1


async def test_a_retry_returns_200_changes_nothing_and_never_reactivates(alice, sessionmaker):
    room_id = await new_room(alice)
    rid = new_id()
    first = await create_doc(alice, room_id, "A", rid=rid)
    other = (await create_doc(alice, room_id, "B")).json()  # B is now active
    before = await events_of(sessionmaker, room_id)
    again = await create_doc(alice, room_id, "A", rid=rid)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["is_active"] is False  # the retry did NOT make A active again
    room = await room_row(sessionmaker, room_id)
    assert str(room["active_document_id"]) == other["id"] and room["active_document_version"] == 2
    assert await events_of(sessionmaker, room_id) == before
    assert await scalar(sessionmaker, "SELECT count(*) FROM documents") == 2


async def test_reusing_a_request_id_with_different_fields_is_409(alice, sessionmaker):
    room_id = await new_room(alice)
    rid = new_id()
    assert (await create_doc(alice, room_id, "A", rid=rid)).status_code == 201
    for title, template in (("Different", "blank"), ("A", "meeting_notes")):
        response = await create_doc(alice, room_id, title, template, rid=rid)
        assert (response.status_code, response.json()["error"]["code"]) == (
            409, "document_request_id_reused",
        )  # fmt: skip
    assert await scalar(sessionmaker, "SELECT count(*) FROM documents") == 1


async def test_concurrent_creates_with_one_request_id_make_exactly_one_document(
    alice, sessionmaker
):
    room_id = await new_room(alice)
    rid = new_id()
    responses = await asyncio.gather(
        *[create_doc(alice, room_id, "Race", rid=rid) for _ in range(6)]
    )
    assert sorted(r.status_code for r in responses) == [200] * 5 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert await scalar(sessionmaker, "SELECT count(*) FROM documents") == 1
    types = [e["type"] for e in await events_of(sessionmaker, room_id)]
    assert types == ["room_created", "document_created", "active_document_changed"]


async def test_the_fingerprint_is_keyed_and_the_same_request_id_may_be_used_by_another_user(
    alice, sessionmaker, settings
):
    fp = documents_service.compute_create_fingerprint(settings, "A", "blank")
    assert len(fp) == 32 and fp != documents_service.compute_create_fingerprint(
        settings, "A", "meeting_notes"
    )
    other = make_settings(message_fingerprint_secret="x" * 40)
    assert fp != documents_service.compute_create_fingerprint(other, "A", "blank")
    room_id = await new_room(alice)
    await create_doc(alice, room_id, "A")
    stored = await scalar(sessionmaker, "SELECT request_fingerprint FROM documents")
    assert stored != b"A" and b"A" not in bytes(stored)


async def test_the_per_room_document_cap_is_enforced_but_retries_still_work(
    alice, sessionmaker, make_actor, settings
):
    cfg = settings.model_copy(update={"max_documents_per_room": 2})
    owner = await make_actor("Cap", app_settings=cfg)
    room_id = await new_room(owner)
    rid = new_id()
    assert (await create_doc(owner, room_id, "1", rid=rid)).status_code == 201
    assert (await create_doc(owner, room_id, "2")).status_code == 201
    third = await create_doc(owner, room_id, "3")
    assert (third.status_code, third.json()["error"]["code"]) == (409, "document_limit_reached")
    assert (await create_doc(owner, room_id, "1", rid=rid)).status_code == 200  # a retry is not new
    assert await scalar(sessionmaker, "SELECT count(*) FROM documents") == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"title": "", "template_key": "blank"},
        {"title": "   ", "template_key": "blank"},
        {"title": "x" * 201, "template_key": "blank"},
        {"title": "tab\there", "template_key": "blank"},
        {"title": "ok", "template_key": "imported"},
        {"title": "ok", "template_key": "nope"},
        {"title": "ok"},
        {"title": 5, "template_key": "blank"},
        {"title": "ok", "template_key": "blank", "created_by": "x"},
        {"title": "ok", "template_key": "blank", "content": {}},
    ],
)
async def test_invalid_creation_bodies_are_422_and_create_nothing(alice, sessionmaker, bad):
    room_id = await new_room(alice)
    response = await alice.post(
        f"/rooms/{room_id}/documents", json={"client_request_id": new_id(), **bad}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")
    assert await scalar(sessionmaker, "SELECT count(*) FROM documents") == 0


async def test_a_title_can_be_exactly_200_code_points_including_emoji(alice, sessionmaker):
    room_id = await new_room(alice)
    title = "😀" * 200
    response = await create_doc(alice, room_id, title)
    assert response.status_code == 201 and response.json()["title"] == title


# ---- reading ----------------------------------------------------------------------------------------


async def test_list_has_metadata_only_pagination_and_never_selects_content(
    alice, sessionmaker, monkeypatch
):
    room_id = await new_room(alice)
    ids = [(await create_doc(alice, room_id, f"Doc {i}")).json()["id"] for i in range(5)]
    seen_sql = []
    from sqlalchemy import event

    engine = sessionmaker.kw["bind"].sync_engine

    def grab(conn, cursor, statement, *args):
        seen_sql.append(statement)

    event.listen(engine, "before_cursor_execute", grab)
    first = (await alice.get(f"/rooms/{room_id}/documents?limit=2")).json()
    second = (
        await alice.get(f"/rooms/{room_id}/documents?limit=2&cursor={first['next_cursor']}")
    ).json()
    third = (
        await alice.get(f"/rooms/{room_id}/documents?limit=2&cursor={second['next_cursor']}")
    ).json()
    event.remove(engine, "before_cursor_execute", grab)
    paged = [d["id"] for page in (first, second, third) for d in page["items"]]
    assert paged == list(reversed(ids)) and third["next_cursor"] is None
    assert all(set(d) == SUMMARY_FIELDS for d in first["items"])
    list_statements = [s for s in seen_sql if "FROM documents" in s and "LIMIT" in s]
    assert list_statements and not any("documents.content," in s or "documents.content " in s
                                       for s in list_statements)  # fmt: skip
    assert (await alice.get(f"/rooms/{room_id}/documents?limit=101")).status_code == 422
    assert (await alice.get(f"/rooms/{room_id}/documents?cursor=bogus")).json()["error"][
        "code"
    ] == "invalid_cursor"


async def test_get_returns_metadata_how_to_open_it_and_the_callers_permission(
    alice, bob, carol, make_actor, sessionmaker
):
    room_id, doc, actors = await roles_room(alice, bob, carol, make_actor, sessionmaker)
    for role, expected in (("owner", True), ("editor", True), ("viewer", False)):
        response = await actors[role].get(f"/rooms/{room_id}/documents/{doc['id']}")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == DOC_FIELDS and body["can_edit"] is expected
        assert body["document_name"] == f"doc:{doc['id']}" and body["collab_path"] == "/collab"
        assert "content" not in body


async def test_etag_and_conditional_get(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    url = f"/rooms/{room_id}/documents/{doc['id']}"
    first = await alice.get(url)
    etag = first.headers["etag"]
    assert etag == '"m1-legacy"' and first.headers["cache-control"] == "no-store"
    for header in (etag, "W/" + etag, f'"x", {etag}', "*"):
        cached = await alice.get(url, headers={"If-None-Match": header})
        assert (cached.status_code, cached.content) == (304, b"")
        assert cached.headers["etag"] == etag
    assert (await alice.get(url, headers={"If-None-Match": '"m9-legacy"'})).status_code == 200
    # a rename changes the ETag (metadata_version); so does the collaboration state
    await alice.patch(url, json={"title": "Renamed"})
    assert (await alice.get(url, headers={"If-None-Match": etag})).status_code == 200
    assert (await alice.get(url)).headers["etag"] == '"m2-legacy"'
    await execute(
        sessionmaker,
        "UPDATE documents SET collab_state = 'needs_repair' WHERE id = :d",
        d=doc["id"],
    )
    assert (await alice.get(url)).headers["etag"] == '"m2-needs_repair"'
    assert (await alice.get(url)).json()["collab_state"] == "needs_repair"


async def test_inactive_documents_stay_readable(alice, bob, sessionmaker):
    room_id, first = await room_with_doc(alice, sessionmaker, (bob, "viewer"))
    await create_doc(alice, room_id, "Newer")
    response = await bob.get(f"/rooms/{room_id}/documents/{first['id']}")
    assert response.status_code == 200 and response.json()["is_active"] is False


# ---- rename -----------------------------------------------------------------------------------------


async def test_rename_bumps_metadata_version_only_and_records_an_event(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    before = await rows(sessionmaker, "SELECT * FROM documents")
    response = await alice.patch(f"/rooms/{room_id}/documents/{doc['id']}", json={"title": " New "})
    assert response.status_code == 200
    body = response.json()
    assert (body["title"], body["metadata_version"]) == ("New", 2)
    [after] = await rows(sessionmaker, "SELECT * FROM documents")
    assert after["content_updated_at"] == before[0]["content_updated_at"]
    assert after["updated_at"] > before[0]["updated_at"]
    assert after["content_hash"] == before[0]["content_hash"]
    last = (await events_of(sessionmaker, room_id))[-1]
    assert (last["type"], last["payload"]) == ("document_renamed", {"document_id": doc["id"]})
    assert "New" not in str(last)


async def test_a_no_op_rename_writes_nothing_and_emits_nothing(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    before = (
        await rows(sessionmaker, "SELECT * FROM documents"),
        await events_of(sessionmaker, room_id),
    )
    response = await alice.patch(f"/rooms/{room_id}/documents/{doc['id']}", json={"title": "Plan"})
    assert response.status_code == 200 and response.json()["metadata_version"] == 1
    assert (
        await rows(sessionmaker, "SELECT * FROM documents"),
        await events_of(sessionmaker, room_id),
    ) == before


async def test_rename_rejects_extra_fields_and_bad_titles(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    url = f"/rooms/{room_id}/documents/{doc['id']}"
    for body in ({"title": ""}, {"title": "x" * 201}, {"title": "a", "content": {}}, {}):
        assert (await alice.patch(url, json=body)).status_code == 422


# ---- active document selection -------------------------------------------------------------------------


async def test_the_owner_selects_an_older_document_and_it_is_recorded(alice, sessionmaker):
    room_id, first = await room_with_doc(alice, sessionmaker)
    await create_doc(alice, room_id, "Second")
    response = await alice.put(
        f"/rooms/{room_id}/active-document", json={"document_id": first["id"]}
    )
    assert response.status_code == 200
    assert response.json() == {"active_document_id": first["id"], "active_document_version": 3}
    last = (await events_of(sessionmaker, room_id))[-1]
    assert (last["type"], last["payload"]) == (
        "active_document_changed", {"document_id": first["id"], "selection_version": 3},
    )  # fmt: skip
    assert (await room_row(sessionmaker, room_id))["version"] == 0


async def test_selecting_the_already_active_document_is_a_no_op(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    before = await events_of(sessionmaker, room_id)
    response = await alice.put(f"/rooms/{room_id}/active-document", json={"document_id": doc["id"]})
    assert response.json() == {"active_document_id": doc["id"], "active_document_version": 1}
    assert await events_of(sessionmaker, room_id) == before


async def test_a_stale_expected_selection_version_is_a_conflict(alice, sessionmaker):
    room_id, first = await room_with_doc(alice, sessionmaker)
    second = (await create_doc(alice, room_id, "Second")).json()
    response = await alice.put(
        f"/rooms/{room_id}/active-document",
        json={"document_id": first["id"], "expected_selection_version": 1},
    )
    error = response.json()["error"]
    assert (response.status_code, error["code"], error["current_selection_version"]) == (
        409, "selection_conflict", 2,
    )  # fmt: skip
    assert str((await room_row(sessionmaker, room_id))["active_document_id"]) == second["id"]
    ok = await alice.put(
        f"/rooms/{room_id}/active-document",
        json={"document_id": first["id"], "expected_selection_version": 2},
    )
    assert ok.status_code == 200 and ok.json()["active_document_version"] == 3


async def test_selecting_a_document_from_another_room_is_404_and_changes_nothing(
    alice, bob, sessionmaker
):
    room_a, doc_a = await room_with_doc(alice, sessionmaker)
    room_b, doc_b = await room_with_doc(bob, sessionmaker)
    response = await alice.put(
        f"/rooms/{room_a}/active-document", json={"document_id": doc_b["id"]}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (404, "document_not_found")
    assert str((await room_row(sessionmaker, room_a))["active_document_id"]) == doc_a["id"]
    unknown = await alice.put(f"/rooms/{room_a}/active-document", json={"document_id": new_id()})
    assert unknown.status_code == 404


# ---- permissions and cross-room isolation ----------------------------------------------------------------


async def test_permission_matrix_for_every_route(alice, bob, carol, make_actor, sessionmaker):
    room_id, doc, actors = await roles_room(alice, bob, carol, make_actor, sessionmaker)
    d = f"/rooms/{room_id}/documents/{doc['id']}"

    async def call(role, kind):
        actor = actors[role]
        if kind == "list":
            return await actor.get(f"/rooms/{room_id}/documents")
        if kind == "get":
            return await actor.get(d)
        if kind == "create":
            return await create_doc(actor, room_id, "x")
        if kind == "rename":
            return await actor.patch(d, json={"title": f"t-{role}"})
        if kind == "select":
            return await actor.put(
                f"/rooms/{room_id}/active-document", json={"document_id": doc["id"]}
            )
        # the old content-save route is gone for everyone
        return await actor.put(f"{d}/content", json={"junk": 1})

    expected = {
        "list": dict(owner=200, editor=200, viewer=200, removed=404, stranger=404),
        "get": dict(owner=200, editor=200, viewer=200, removed=404, stranger=404),
        "create": dict(owner=201, editor=403, viewer=403, removed=404, stranger=404),
        "rename": dict(owner=200, editor=403, viewer=403, removed=404, stranger=404),
        "select": dict(owner=200, editor=403, viewer=403, removed=404, stranger=404),
        "save": dict(owner=404, editor=404, viewer=404, removed=404, stranger=404),
    }
    for kind in ("list", "get", "create", "rename", "select", "save"):
        for role in ("viewer", "editor", "removed", "stranger", "owner"):
            response = await call(role, kind)
            assert response.status_code == expected[kind][role], (kind, role, response.text)
            if response.status_code == 403:
                assert response.json()["error"]["code"] == "insufficient_role"
            if response.status_code == 404 and kind != "save":
                assert response.json()["error"]["code"] == "room_not_found"


async def test_unauthenticated_and_csrfless_requests_are_rejected(alice, sessionmaker, client):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    d = f"/rooms/{room_id}/documents/{doc['id']}"
    assert (await client.get(d)).status_code == 401
    assert (await client.get(f"/rooms/{room_id}/documents")).status_code == 401
    for call in (
        alice.post(
            f"/rooms/{room_id}/documents",
            json={"title": "x", "template_key": "blank", "client_request_id": new_id()},
            csrf=False,
        ),
        alice.patch(d, json={"title": "x"}, csrf=False),
        alice.put(f"/rooms/{room_id}/active-document", json={"document_id": doc["id"]}, csrf=False),
    ):
        response = await call
        assert (response.status_code, response.json()["error"]["code"]) == (403, "csrf_failed")


async def test_a_document_of_another_room_is_not_found_through_any_route(alice, bob, sessionmaker):
    room_a, _doc_a = await room_with_doc(alice, sessionmaker)
    room_b, doc_b = await room_with_doc(bob, sessionmaker)
    await add_member(
        sessionmaker, room_b, alice.id, "editor"
    )  # alice can see B; A URLs are still A
    path = f"/rooms/{room_a}/documents/{doc_b['id']}"
    assert (await alice.get(path)).json()["error"]["code"] == "document_not_found"
    assert (await alice.patch(path, json={"title": "stolen"})).json()["error"][
        "code"
    ] == "document_not_found"
    stored = await rows(sessionmaker, "SELECT title FROM documents WHERE id = :d", d=doc_b["id"])
    assert stored == [{"title": "Plan"}]
    for junk in ("not-a-uuid", "0" * 36, str(uuid.uuid4())):
        assert (await alice.get(f"/rooms/{room_a}/documents/{junk}")).status_code == 404


async def test_a_nonmember_sending_an_invalid_body_gets_404_not_422(alice, bob, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    assert (await bob.post(f"/rooms/{room_id}/documents", json={"junk": 1})).status_code == 404


# ---- rooms representation ----------------------------------------------------------------------------------


async def test_the_room_representation_carries_the_active_document(alice, sessionmaker):
    room_id = await new_room(alice)
    empty = (await alice.get(f"/rooms/{room_id}")).json()
    assert (empty["active_document_id"], empty["active_document_version"]) == (None, 0)
    doc = (await create_doc(alice, room_id)).json()
    room = (await alice.get(f"/rooms/{room_id}")).json()
    assert (room["active_document_id"], room["active_document_version"]) == (doc["id"], 1)
    listed = (await alice.get("/rooms")).json()["items"][0]
    assert listed["active_document_id"] == doc["id"]
