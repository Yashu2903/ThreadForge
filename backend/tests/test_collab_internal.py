"""The /internal/collab API: secret, fenced lease, authorization, Yjs state, seeding, snapshots,
marks and the operator restore. Real PostgreSQL; the HTTP layer is the real ASGI app."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.services import collab
from tests.doc_helpers import room_with_doc
from tests.factories import TEST_INTERNAL_SECRET, make_settings
from tests.helpers import add_member, execute, make_client, rows, scalar

SECRET = {"X-Internal-Secret": TEST_INTERNAL_SECRET}


@pytest_asyncio.fixture
async def internal(settings, engine, google):
    """A client that is NOT a browser: no cookies, just the shared secret."""
    async with make_client(settings, engine, google) as c:
        c.headers.update(SECRET)
        yield c


async def acquire(internal, holder="holder-aaaaaaaa") -> int:
    response = await internal.post("/internal/collab/lease/acquire", json={"holder_id": holder})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def lease(token: int) -> dict[str, str]:
    return {"X-Collab-Lease": str(token)}


def put_headers(token: int, counter: int, *, seed: bool = False) -> dict[str, str]:
    headers = {**lease(token), "X-Update-Counter": str(counter)}
    if seed:
        headers["X-Insert-If-Absent"] = "true"
    return headers


def tiptap_doc(*texts: str, image: str | None = None) -> dict:
    blocks = [
        {
            "type": "paragraph",
            "attrs": {"id": str(uuid.uuid4())},
            "content": [{"type": "text", "text": t}],
        }
        for t in texts
    ]
    if image:
        blocks.append(
            {
                "type": "paragraph",
                "attrs": {"id": str(uuid.uuid4())},
                "content": [
                    {
                        "type": "image",
                        "attrs": {"assetId": image, "alt": "", "width": 10, "height": 10},
                    }
                ],
            }
        )
    return {"type": "doc", "content": blocks}


def snapshot_body(doc: dict, *, kind="periodic", counter=1) -> dict:
    return {
        "kind": kind,
        "yjs_update_counter": counter,
        "content": doc,
        "content_hash": hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest(),
        "text_length": 5,
    }


async def seeded(internal, sessionmaker, alice, *members):
    room_id, doc = await room_with_doc(alice, sessionmaker, *members)
    token = await acquire(internal)
    response = await internal.put(
        f"/internal/collab/documents/{doc['id']}/state",
        content=b"seed-bytes",
        headers=put_headers(token, 0, seed=True),
    )
    assert response.status_code == 204, response.text
    return room_id, doc, token


async def insert_asset(sm, room_id, owner_id, state="pending") -> str:
    asset_id = str(uuid.uuid4())
    await execute(
        sm,
        "INSERT INTO room_assets (id, room_id, uploaded_by, origin, state, media_type, byte_size,"
        " sha256, width_px, height_px, storage_key, created_at, attached_at)"
        " VALUES (:i, :r, :u, 'editor_upload', CAST(:s AS varchar), 'image/png', 10, decode(:h, 'hex'), 10, 10,"
        " :k, now(), CASE WHEN CAST(:s AS varchar) = 'attached' THEN now() END)",
        i=asset_id,
        r=room_id,
        u=owner_id,
        s=state,
        h="ab" * 32,
        k=uuid.uuid4().hex,
    )
    return asset_id


# ---- the shared secret ---------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Secret": "wrong"}, {"X-Internal-Secret": ""}])
async def test_every_internal_route_refuses_a_missing_or_wrong_secret(
    settings, engine, google, headers
):
    async with make_client(settings, engine, google) as c:
        for method, path in (
            ("POST", "/internal/collab/lease/acquire"),
            ("POST", "/internal/collab/authorize"),
            ("GET", f"/internal/collab/documents/{uuid.uuid4()}/state"),
            ("PUT", f"/internal/collab/documents/{uuid.uuid4()}/state"),
            ("GET", f"/internal/collab/documents/{uuid.uuid4()}/seed"),
            ("POST", f"/internal/collab/documents/{uuid.uuid4()}/snapshot"),
            ("POST", f"/internal/collab/documents/{uuid.uuid4()}/mark"),
            ("POST", "/internal/collab/assets/check"),
        ):
            response = await c.request(method, path, headers=headers, json={})
            assert response.status_code == 401, (method, path)
            assert response.json()["error"]["code"] == "unauthenticated"


async def test_a_non_ascii_secret_is_a_clean_401_not_a_500(internal):
    response = await internal.post(
        "/internal/collab/lease/acquire",
        json={"holder_id": "holder-aaaaaaaa"},
        headers={"X-Internal-Secret": "säcret-ü".encode("latin-1")},
    )
    assert response.status_code == 401


async def test_without_a_configured_secret_the_internal_api_is_off_not_open(engine, google):
    off = make_settings(internal_api_secret="")
    async with make_client(off, engine, google) as c:
        response = await c.post(
            "/internal/collab/lease/acquire",
            json={"holder_id": "holder-aaaaaaaa"},
            headers=SECRET,
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal_api_disabled"


def test_production_requires_a_real_internal_secret():
    from tests.factories import make_production_settings

    with pytest.raises(ValueError, match="INTERNAL_API_SECRET"):
        make_production_settings(internal_api_secret="")
    with pytest.raises(ValueError, match="INTERNAL_API_SECRET"):
        make_production_settings(internal_api_secret="short")
    assert make_production_settings(internal_api_secret="x" * 40)


async def test_internal_routes_are_not_in_the_public_openapi(internal):
    response = await internal.get("/openapi.json")
    assert "/internal/collab" not in response.text


# ---- the lease ------------------------------------------------------------------------------------


async def test_the_lease_is_exclusive_renewable_and_fenced(internal, sessionmaker):
    first = await acquire(internal, "holder-one-aaaa")
    assert first == 1
    refused = await internal.post(
        "/internal/collab/lease/acquire", json={"holder_id": "holder-two-bbbb"}
    )
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "lease_held")

    renewed = await internal.post(
        "/internal/collab/lease/renew", json={"holder_id": "holder-one-aaaa", "token": first}
    )
    assert renewed.status_code == 200 and renewed.json()["token"] == first

    # an expired lease can be taken; the token increases; the old holder is fenced out
    await execute(sessionmaker, "UPDATE collab_lease SET expires_at = now() - interval '1 second'")
    second = await acquire(internal, "holder-two-bbbb")
    assert second == 2
    stale = await internal.post(
        "/internal/collab/lease/renew", json={"holder_id": "holder-one-aaaa", "token": first}
    )
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "stale_lease")


async def test_a_stale_token_cannot_write_anything(internal, sessionmaker, alice):
    room_id, doc, token = await seeded(internal, sessionmaker, alice)
    await execute(sessionmaker, "UPDATE collab_lease SET expires_at = now() - interval '1 second'")
    assert await acquire(internal, "holder-two-bbbb") == token + 1
    path = f"/internal/collab/documents/{doc['id']}"
    for response in (
        await internal.put(f"{path}/state", content=b"x", headers=put_headers(token, 5)),
        await internal.post(
            f"{path}/snapshot", json=snapshot_body(tiptap_doc("a")), headers=lease(token)
        ),
        await internal.post(
            f"{path}/mark", json={"state": "needs_repair", "reason": "x"}, headers=lease(token)
        ),
    ):
        assert (response.status_code, response.json()["error"]["code"]) == (409, "stale_lease")
    assert await scalar(sessionmaker, "SELECT collab_state FROM documents") == "ready"


async def test_release_frees_the_lease_immediately(internal):
    token = await acquire(internal, "holder-one-aaaa")
    response = await internal.post(
        "/internal/collab/lease/release", json={"holder_id": "holder-one-aaaa", "token": token}
    )
    assert response.status_code == 204
    assert await acquire(internal, "holder-two-bbbb") == token + 1


async def test_the_lease_table_holds_only_one_row(internal, sessionmaker):
    await acquire(internal)
    with pytest.raises(IntegrityError):
        await execute(
            sessionmaker,
            "INSERT INTO collab_lease (id, fencing_token, expires_at) VALUES (2, 1, now())",
        )


# ---- authorize ------------------------------------------------------------------------------------


async def post_authorize(internal, settings: Settings, secret: str | None, doc_id: str):
    header = f"{settings.session_cookie_name}={secret}" if secret else None
    return await internal.post(
        "/internal/collab/authorize", json={"cookie_header": header, "document_id": doc_id}
    )


async def test_authorize_returns_identity_role_and_can_edit_but_never_the_cookie(
    internal, settings, sessionmaker, alice, bob, carol, make_actor
):
    dave, erin = await make_actor("Dave"), await make_actor("Erin")
    room_id, doc = await room_with_doc(
        alice, sessionmaker, (bob, "editor"), (carol, "viewer"), (dave, "editor")
    )
    await add_member(sessionmaker, room_id, dave.id, "editor", state="removed")
    expected = {alice: ("owner", True), bob: ("editor", True), carol: ("viewer", False)}
    for actor, (role, can_edit) in expected.items():
        response = await post_authorize(internal, settings, actor.secret, doc["id"])
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["role"], body["can_edit"]) == (role, can_edit)
        assert body["user_id"] == str(actor.id) and body["display_name"] == actor.name
        assert body["session_id"] == str(actor.session_id)
        assert body["room_id"] == room_id and body["collab_state"] == "legacy"
        assert actor.secret not in response.text
        assert set(body) == {
            "user_id", "display_name", "session_id", "role", "can_edit", "room_id",
            "document_id", "collab_state",
        }  # fmt: skip
    for outsider in (dave, erin):  # removed member, stranger
        response = await post_authorize(internal, settings, outsider.secret, doc["id"])
        assert (response.status_code, response.json()["error"]["code"]) == (403, "forbidden")


async def test_authorize_distinguishes_invalid_sessions_from_missing_access(
    internal, settings, sessionmaker, alice
):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    for header in (None, "", "tf_session=nope", "other=1", "x" * 9000):
        response = await internal.post(
            "/internal/collab/authorize", json={"cookie_header": header, "document_id": doc["id"]}
        )
        assert response.status_code in (401, 422)
        if response.status_code == 401:
            assert response.json()["error"]["code"] == "session_invalid"
    # revoked session and disabled user
    await execute(
        sessionmaker, "UPDATE sessions SET revoked_at = now() WHERE id = :i", i=alice.session_id
    )
    response = await post_authorize(internal, settings, alice.secret, doc["id"])
    assert response.status_code == 401


async def test_authorize_refuses_a_disabled_user_and_a_missing_document(
    internal, settings, sessionmaker, alice, bob
):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"))
    response = await post_authorize(internal, settings, alice.secret, str(uuid.uuid4()))
    assert response.status_code == 403  # no existence oracle
    await execute(sessionmaker, "UPDATE users SET state = 'disabled' WHERE id = :i", i=bob.id)
    response = await post_authorize(internal, settings, bob.secret, doc["id"])
    assert response.status_code == 401


# ---- state, seeding ---------------------------------------------------------------------------------


async def test_an_unseeded_document_has_no_state_and_a_seed_source(internal, sessionmaker, alice):
    room_id, doc = await room_with_doc(alice, sessionmaker, template="project_brief")
    state = await internal.get(f"/internal/collab/documents/{doc['id']}/state")
    assert state.status_code == 200 and state.content == b""
    assert state.headers["X-Collab-State"] == "legacy" and state.headers["X-Update-Counter"] == "-1"
    assert state.headers["X-Room-Id"] == room_id
    seed = await internal.get(f"/internal/collab/documents/{doc['id']}/seed")
    assert seed.status_code == 200
    body = seed.json()
    assert body["format"] == "interchange" and body["content"]["schema_version"] == 1
    assert body["room_id"] == room_id
    missing = await internal.get(f"/internal/collab/documents/{uuid.uuid4()}/state")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "document_not_found")


async def test_seeding_stores_the_state_and_marks_the_document_ready_in_one_step(
    internal, sessionmaker, alice
):
    room_id, doc, token = await seeded(internal, sessionmaker, alice)
    assert await scalar(sessionmaker, "SELECT collab_state FROM documents") == "ready"
    stored = await internal.get(f"/internal/collab/documents/{doc['id']}/state")
    assert stored.content == b"seed-bytes" and stored.headers["X-Update-Counter"] == "0"
    assert stored.headers["X-Collab-State"] == "ready"
    gone = await internal.get(f"/internal/collab/documents/{doc['id']}/seed")
    assert (gone.status_code, gone.json()["error"]["code"]) == (409, "already_seeded")


async def test_concurrent_seeding_inserts_exactly_one_state(internal, sessionmaker, alice):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    token = await acquire(internal)
    path = f"/internal/collab/documents/{doc['id']}/state"
    results = await asyncio.gather(
        *[
            internal.put(
                path, content=f"candidate-{n}".encode(), headers=put_headers(token, 0, seed=True)
            )
            for n in range(6)
        ]
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [204, 409, 409, 409, 409, 409]
    assert {r.json()["error"]["code"] for r in results if r.status_code == 409} == {
        "already_seeded"
    }
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_yjs_state") == 1


async def test_store_needs_a_seeded_document_and_a_rising_counter(internal, sessionmaker, alice):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    token = await acquire(internal)
    path = f"/internal/collab/documents/{doc['id']}/state"
    early = await internal.put(path, content=b"x", headers=put_headers(token, 1))
    assert (early.status_code, early.json()["error"]["code"]) == (409, "not_seeded")
    assert (
        await internal.put(path, content=b"s", headers=put_headers(token, 0, seed=True))
    ).status_code == 204
    assert (
        await internal.put(path, content=b"one", headers=put_headers(token, 1))
    ).status_code == 204
    again = await internal.put(path, content=b"dup", headers=put_headers(token, 1))
    assert (again.status_code, again.json()["error"]["code"]) == (409, "stale_counter")
    assert again.json()["error"]["current_counter"] == 1
    older = await internal.put(path, content=b"old", headers=put_headers(token, 0))
    assert older.status_code == 409
    assert (
        await internal.put(path, content=b"three", headers=put_headers(token, 3))
    ).status_code == 204
    stored = await internal.get(path)
    assert stored.content == b"three" and stored.headers["X-Update-Counter"] == "3"
    assert await scalar(sessionmaker, "SELECT byte_size FROM document_yjs_state") == 5


async def test_stores_never_touch_documents_content_or_emit_room_events(
    internal, sessionmaker, alice
):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    before = (
        await rows(sessionmaker, "SELECT content, content_hash, content_version FROM documents")
    )[0]
    events = await scalar(sessionmaker, "SELECT count(*) FROM room_events")
    token = await acquire(internal)
    path = f"/internal/collab/documents/{doc['id']}/state"
    await internal.put(path, content=b"s", headers=put_headers(token, 0, seed=True))
    for n in range(1, 4):
        await internal.put(path, content=b"edit-%d" % n, headers=put_headers(token, n))
    after = (
        await rows(sessionmaker, "SELECT content, content_hash, content_version FROM documents")
    )[0]
    assert after == before  # documents.content is read-only seed material
    assert await scalar(sessionmaker, "SELECT count(*) FROM room_events") == events


async def test_a_state_over_the_configured_maximum_is_refused(engine, google, sessionmaker, alice):
    small = make_settings(collab_max_state_bytes=64 * 1024)
    async with make_client(small, engine, google) as c:
        c.headers.update(SECRET)
        room_id, doc = await room_with_doc(alice, sessionmaker)
        token = await acquire(c)
        path = f"/internal/collab/documents/{doc['id']}/state"
        ok = await c.put(path, content=b"s" * 1000, headers=put_headers(token, 0, seed=True))
        assert ok.status_code == 204
        big = await c.put(path, content=b"b" * (64 * 1024 + 1), headers=put_headers(token, 1))
        assert big.status_code == 413
    assert await scalar(sessionmaker, "SELECT byte_size FROM document_yjs_state") == 1000


async def test_the_database_rejects_a_state_whose_size_or_room_is_wrong(
    internal, sessionmaker, alice, bob
):
    room_id, doc, _ = await seeded(internal, sessionmaker, alice)
    with pytest.raises(IntegrityError):
        await execute(sessionmaker, "UPDATE document_yjs_state SET byte_size = 1")
    other_room, other_doc = await room_with_doc(bob, sessionmaker)
    with pytest.raises(IntegrityError):  # composite foreign key: the room must be the document's
        await execute(
            sessionmaker,
            "INSERT INTO document_yjs_state (document_id, room_id, state, update_counter, byte_size,"
            " updated_at) VALUES (:d, :r, '\\x00', 0, 1, now())",
            d=other_doc["id"],
            r=room_id,
        )


# ---- snapshots and asset references -------------------------------------------------------------------


async def test_a_snapshot_is_stored_deduplicated_and_pruned_keeping_the_newest_seed(
    internal, sessionmaker, alice
):
    room_id, doc, token = await seeded(internal, sessionmaker, alice)
    path = f"/internal/collab/documents/{doc['id']}/snapshot"
    seed = await internal.post(
        path, json=snapshot_body(tiptap_doc("seed"), kind="seed", counter=0), headers=lease(token)
    )
    assert seed.status_code == 200 and seed.json()["stored"] is True
    body = snapshot_body(tiptap_doc("first"), counter=1)
    first = await internal.post(path, json=body, headers=lease(token))
    assert first.json()["stored"] is True
    same = await internal.post(path, json=body, headers=lease(token))
    assert same.json() == {"stored": False, "snapshot_id": None}  # unchanged content: no new row
    for n in range(2, 16):
        r = await internal.post(
            path, json=snapshot_body(tiptap_doc(f"v{n}"), counter=n), headers=lease(token)
        )
        assert r.status_code == 200
    kinds = await rows(
        sessionmaker, "SELECT kind, yjs_update_counter FROM document_snapshots ORDER BY created_at"
    )
    assert len(kinds) == 11  # newest 10 + the seed that fell out of the window
    assert kinds[0]["kind"] == "seed" and kinds[-1]["yjs_update_counter"] == 15


async def test_snapshot_asset_references_follow_the_document_and_the_asset_lifecycle(
    internal, sessionmaker, alice
):
    room_id, doc, token = await seeded(internal, sessionmaker, alice)
    path = f"/internal/collab/documents/{doc['id']}/snapshot"
    one = await insert_asset(sessionmaker, room_id, alice.id)
    two = await insert_asset(sessionmaker, room_id, alice.id)
    assert (
        await internal.post(
            path, json=snapshot_body(tiptap_doc("a", image=one), counter=1), headers=lease(token)
        )
    ).status_code == 200
    refs = await rows(sessionmaker, "SELECT asset_id::text AS a FROM document_asset_refs")
    assert [r["a"] for r in refs] == [one]
    assert (
        await scalar(sessionmaker, "SELECT state FROM room_assets WHERE id = :i", i=one)
        == "attached"
    )
    # swap the image: the old one becomes unreferenced (kept for the grace period), the new attached
    await internal.post(
        path, json=snapshot_body(tiptap_doc("a", image=two), counter=2), headers=lease(token)
    )
    refs = await rows(sessionmaker, "SELECT asset_id::text AS a FROM document_asset_refs")
    assert [r["a"] for r in refs] == [two]
    old = (
        await rows(
            sessionmaker, "SELECT state, unreferenced_since FROM room_assets WHERE id = :i", i=one
        )
    )[0]
    assert old["state"] == "attached" and old["unreferenced_since"] is not None
    # re-adding the first clears unreferenced_since
    await internal.post(
        path, json=snapshot_body(tiptap_doc("a", image=one), counter=3), headers=lease(token)
    )
    assert (
        await scalar(
            sessionmaker, "SELECT unreferenced_since FROM room_assets WHERE id = :i", i=one
        )
    ) is None


async def test_a_snapshot_referencing_a_missing_expired_or_foreign_asset_is_refused_whole(
    internal, sessionmaker, alice, bob
):
    room_id, doc, token = await seeded(internal, sessionmaker, alice)
    other_room, _ = await room_with_doc(bob, sessionmaker)
    foreign = await insert_asset(sessionmaker, other_room, bob.id)
    expired = await insert_asset(sessionmaker, room_id, alice.id, state="expired")
    path = f"/internal/collab/documents/{doc['id']}/snapshot"
    for bad in (str(uuid.uuid4()), foreign, expired):
        response = await internal.post(
            path, json=snapshot_body(tiptap_doc("a", image=bad), counter=1), headers=lease(token)
        )
        assert (response.status_code, response.json()["error"]["code"]) == (422, "asset_not_found")
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_snapshots") == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_asset_refs") == 0
    bad_id = await internal.post(
        path,
        json=snapshot_body(tiptap_doc("a", image="not-a-uuid"), counter=1),
        headers=lease(token),
    )
    assert bad_id.status_code == 422


async def test_snapshots_need_a_seeded_document_and_a_valid_shape(internal, sessionmaker, alice):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    token = await acquire(internal)
    path = f"/internal/collab/documents/{doc['id']}/snapshot"
    early = await internal.post(path, json=snapshot_body(tiptap_doc("a")), headers=lease(token))
    assert (early.status_code, early.json()["error"]["code"]) == (409, "not_seeded")
    await internal.put(
        f"/internal/collab/documents/{doc['id']}/state",
        content=b"s",
        headers=put_headers(token, 0, seed=True),
    )
    for broken in (
        {**snapshot_body(tiptap_doc("a")), "kind": "restore"},
        {**snapshot_body(tiptap_doc("a")), "content_hash": "zz"},
        {**snapshot_body({"type": "paragraph"})},
        {**snapshot_body(tiptap_doc("a")), "extra": 1},
    ):
        response = await internal.post(path, json=broken, headers=lease(token))
        assert response.status_code == 422, broken


async def test_asset_check_scopes_by_room(internal, sessionmaker, alice, bob):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    other_room, _ = await room_with_doc(bob, sessionmaker)
    mine = await insert_asset(sessionmaker, room_id, alice.id)
    theirs = await insert_asset(sessionmaker, other_room, bob.id)
    gone = await insert_asset(sessionmaker, room_id, alice.id, state="expired")
    response = await internal.post(
        "/internal/collab/assets/check",
        json={"room_id": room_id, "asset_ids": [mine, theirs, gone]},
    )
    assert response.json() == {"missing": sorted([theirs, gone])}


# ---- marks and repair ---------------------------------------------------------------------------------


async def test_needs_repair_blocks_seeding_and_is_visible_to_authorize(
    internal, settings, sessionmaker, alice
):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    token = await acquire(internal)
    base = f"/internal/collab/documents/{doc['id']}"
    marked = await internal.post(
        f"{base}/mark",
        json={"state": "needs_repair", "reason": "seed_invalid"},
        headers=lease(token),
    )
    assert marked.status_code == 204
    seed = await internal.get(f"{base}/seed")
    assert (seed.status_code, seed.json()["error"]["code"]) == (409, "needs_repair")
    seed_write = await internal.put(
        f"{base}/state", content=b"s", headers=put_headers(token, 0, seed=True)
    )
    assert seed_write.status_code == 409 and seed_write.json()["error"]["code"] == "needs_repair"
    body = (await post_authorize(internal, settings, alice.secret, doc["id"])).json()
    assert body["collab_state"] == "needs_repair"
    legacy_ready = await internal.post(
        f"{base}/mark", json={"state": "ready", "reason": "x"}, headers=lease(token)
    )
    assert legacy_ready.status_code == 409  # never 'ready' without Yjs state
    for bad in ("legacy", "bogus"):
        response = await internal.post(
            f"{base}/mark", json={"state": bad, "reason": "x"}, headers=lease(token)
        )
        assert response.status_code == 422


async def test_a_live_document_can_recover_from_needs_repair(internal, sessionmaker, alice):
    room_id, doc, token = await seeded(internal, sessionmaker, alice)
    base = f"/internal/collab/documents/{doc['id']}"
    await internal.post(
        f"{base}/mark", json={"state": "needs_repair", "reason": "x"}, headers=lease(token)
    )
    # raw Yjs is still stored while needs_repair (never lose what people typed)
    assert (
        await internal.put(f"{base}/state", content=b"typed", headers=put_headers(token, 1))
    ).status_code == 204
    assert (
        await internal.post(
            f"{base}/mark", json={"state": "ready", "reason": "valid_again"}, headers=lease(token)
        )
    ).status_code == 204
    assert await scalar(sessionmaker, "SELECT collab_state FROM documents") == "ready"


async def test_restore_requires_a_stopped_service_and_reseeds_from_the_snapshot(
    internal, sessionmaker, alice
):
    room_id, doc, token = await seeded(internal, sessionmaker, alice)
    base = f"/internal/collab/documents/{doc['id']}"
    good = tiptap_doc("the last good text")
    await internal.post(
        f"{base}/snapshot", json=snapshot_body(good, counter=1), headers=lease(token)
    )
    await internal.post(
        f"{base}/mark", json={"state": "needs_repair", "reason": "x"}, headers=lease(token)
    )

    document_id = uuid.UUID(doc["id"])
    with pytest.raises(Exception, match="service_running|Stop the collaboration"):
        await collab.restore_document(sessionmaker, document_id, None)  # lease still live

    await internal.post(
        "/internal/collab/lease/release", json={"holder_id": "holder-aaaaaaaa", "token": token}
    )
    raw, counter = await collab.restore_document(sessionmaker, document_id, None)
    assert raw == b"seed-bytes" and counter == 0
    assert await scalar(sessionmaker, "SELECT collab_state FROM documents") == "legacy"
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_yjs_state") == 0
    new_token = await acquire(internal, "holder-two-bbbb")
    seed = (await internal.get(f"{base}/seed")).json()
    assert seed["format"] == "tiptap" and seed["content"] == good
    # and the document can be seeded again, exactly once
    assert (
        await internal.put(
            f"{base}/state", content=b"fresh", headers=put_headers(new_token, 0, seed=True)
        )
    ).status_code == 204
    # documents.content was never modified by any of this
    assert await scalar(sessionmaker, "SELECT content_version FROM documents") == 1


async def test_the_restore_command_backs_up_first_then_reseeds_from_the_snapshot(
    internal, sessionmaker, alice, tmp_path, monkeypatch
):
    from app.commands.restore_document import main
    from tests.factories import TEST_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    room_id, doc, token = await seeded(internal, sessionmaker, alice)
    base = f"/internal/collab/documents/{doc['id']}"
    good = tiptap_doc("recovered")
    await internal.post(
        f"{base}/snapshot", json=snapshot_body(good, counter=1), headers=lease(token)
    )
    await internal.put(f"{base}/state", content=b"raw-yjs", headers=put_headers(token, 1))
    await internal.post(
        f"{base}/mark", json={"state": "needs_repair", "reason": "x"}, headers=lease(token)
    )

    # refused while the service holds the lease
    assert (
        await asyncio.to_thread(main, ["--document", doc["id"], "--backup-dir", str(tmp_path)]) == 1
    )
    await internal.post(
        "/internal/collab/lease/release", json={"holder_id": "holder-aaaaaaaa", "token": token}
    )
    assert (
        await asyncio.to_thread(main, ["--document", doc["id"], "--backup-dir", str(tmp_path)]) == 0
    )
    assert (tmp_path / f"doc-{doc['id']}-counter-1.ystate").read_bytes() == b"raw-yjs"
    assert await scalar(sessionmaker, "SELECT collab_state FROM documents") == "legacy"
    await acquire(internal, "holder-two-bbbb")
    assert (await internal.get(f"{base}/seed")).json()["content"] == good


# ---- access over time (checkpoint 4) ---------------------------------------------------------------------


async def check_access(internal, *rooms: tuple[str, list[tuple[object, str]]]):
    body = {
        "rooms": [
            {
                "room_id": room,
                "connections": [{"session_id": str(s), "document_id": d} for s, d in conns],
            }
            for room, conns in rooms
        ]
    }
    response = await internal.post("/internal/collab/access/check", json=body)
    assert response.status_code == 200, response.text
    return [
        [(r["status"], r["can_edit"]) for r in room["results"]] for room in response.json()["rooms"]
    ]


async def test_access_check_reports_each_connection_in_order(
    internal, sessionmaker, alice, bob, carol, make_actor
):
    dave = await make_actor("Dave")
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"), (carol, "viewer"))
    conns = [(a.session_id, doc["id"]) for a in (alice, bob, carol, dave)]
    [result] = await check_access(internal, (room_id, conns))
    assert result == [("ok", True), ("ok", True), ("ok", False), ("not_member", False)]
    assert await check_access(internal, (room_id, [])) == [[]]


async def test_access_check_notices_every_kind_of_change(internal, sessionmaker, alice, bob, carol):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"), (carol, "editor"))
    conns = [(a.session_id, doc["id"]) for a in (alice, bob, carol)]
    # role downgrade: still allowed to read, no longer to write
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    # removal
    await add_member(sessionmaker, room_id, carol.id, "editor", state="removed")
    [result] = await check_access(internal, (room_id, conns))
    assert result == [("ok", True), ("ok", False), ("not_member", False)]
    # revoked session, expired session, disabled user, unknown session id
    await execute(
        sessionmaker, "UPDATE sessions SET revoked_at = now() WHERE id = :s", s=alice.session_id
    )
    await execute(
        sessionmaker,
        "UPDATE sessions SET created_at = now() - interval '2 days',"
        " expires_at = now() - interval '1 second' WHERE id = :s",
        s=bob.session_id,
    )
    [result] = await check_access(internal, (room_id, conns))
    assert [r[0] for r in result] == ["session_invalid", "session_invalid", "not_member"]
    await add_member(sessionmaker, room_id, carol.id, "editor")
    await execute(sessionmaker, "UPDATE users SET state = 'disabled' WHERE id = :u", u=carol.id)
    [result] = await check_access(internal, (room_id, conns))
    assert result[2] == ("session_invalid", False)
    [unknown] = await check_access(internal, (room_id, [(uuid.uuid4(), doc["id"])]))
    assert unknown == [("session_invalid", False)]


async def test_access_check_refuses_a_document_of_another_room(internal, sessionmaker, alice, bob):
    room_a, doc_a = await room_with_doc(alice, sessionmaker)
    room_b, doc_b = await room_with_doc(bob, sessionmaker)
    [result] = await check_access(internal, (room_a, [(alice.session_id, doc_b["id"])]))
    assert result == [("document_gone", False)]


async def test_access_check_batches_many_connections_and_rooms_in_few_queries(
    internal, sessionmaker, alice, bob, engine
):
    from sqlalchemy import event

    room_a, doc_a = await room_with_doc(alice, sessionmaker)
    room_b, doc_b = await room_with_doc(bob, sessionmaker)
    statements: list[str] = []
    event.listen(engine.sync_engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
    results = await check_access(
        internal,
        (room_a, [(alice.session_id, doc_a["id"])] * 50),
        (room_b, [(bob.session_id, doc_b["id"])] * 50),
    )
    assert [len(r) for r in results] == [50, 50] and all(
        x == ("ok", True) for r in results for x in r
    )
    queries = [s for s in statements if "sessions" in s or "documents" in s]
    assert len(queries) == 4  # two statements per room, however many connections it has


async def test_access_check_needs_the_secret_and_a_valid_body(engine, google, settings, internal):
    async with make_client(settings, engine, google) as anonymous:
        response = await anonymous.post("/internal/collab/access/check", json={"rooms": []})
        assert response.status_code == 401
    for bad in ({}, {"rooms": [{"room_id": "x", "connections": []}]}, {"rooms": [], "extra": 1}):
        assert (await internal.post("/internal/collab/access/check", json=bad)).status_code == 422
