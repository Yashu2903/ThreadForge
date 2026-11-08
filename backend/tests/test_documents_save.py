"""PUT /rooms/{room}/documents/{doc}/content: validation, versions, conflicts, atomicity, limits."""

import asyncio
import json

import pytest
from sqlalchemy import text

from tests.doc_helpers import content, create_doc, events_of, new_id, paragraph, room_with_doc, save
from tests.helpers import TRUSTED_ORIGIN, add_member, execute, rows, scalar
from tests.test_invitations_api import new_room
from tests.test_messages_send import send


def url(room_id, doc_id):
    return f"/rooms/{room_id}/documents/{doc_id}/content"


async def stored(sm, doc_id):
    [row] = await rows(sm, "SELECT * FROM documents WHERE id = :d", d=doc_id)
    return row


async def bucket_total(sm):
    return await scalar(sm, "SELECT coalesce(sum(count), 0) FROM document_write_buckets")


async def insert_asset(sm, room_id, user_id, state="pending"):
    asset_id = new_id()
    await execute(
        sm,
        "INSERT INTO room_assets (id, room_id, uploaded_by, origin, state, media_type, byte_size,"
        " sha256, width_px, height_px, storage_key, created_at, attached_at)"
        " VALUES (:i, :r, :u, 'editor_upload', CAST(:s AS text), 'image/png', 10, decode(repeat('ab', 32), 'hex'),"
        " 5, 5, :k, clock_timestamp(),"
        " CASE WHEN CAST(:s AS text) = 'pending' THEN NULL ELSE clock_timestamp() END)",
        i=asset_id,
        r=room_id,
        u=user_id,
        s=state,
        k="key-" + asset_id,
    )
    return asset_id


def with_image(asset_id, *, text_value="pic"):
    body = content(text_value)
    body["blocks"][0]["children"].append(
        {"type": "image", "id": new_id(), "asset_id": asset_id, "width_px": 5, "height_px": 5}
    )
    return body


# ---- the happy path ---------------------------------------------------------------------------------------


async def test_a_save_bumps_the_version_updates_the_row_and_records_one_event(
    alice, bob, sessionmaker
):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"))
    before = await stored(sessionmaker, doc["id"])
    body = content("Hello", "World")
    response = await save(bob, room_id, doc["id"], 1, body)
    assert response.status_code == 200
    out = response.json()
    assert set(out) == {
        "content_version",
        "content_hash",
        "canonical_changed",
        "unchanged",
        "content",
    }
    assert (out["content_version"], out["canonical_changed"], out["unchanged"], out["content"]) == (
        2, False, False, None,
    )  # fmt: skip
    after = await stored(sessionmaker, doc["id"])
    assert after["content"] == body and after["content_hash"].hex() == out["content_hash"]
    assert after["content_updated_at"] > before["content_updated_at"]
    assert (after["metadata_version"], after["title"]) == (1, "Plan")
    last = (await events_of(sessionmaker, room_id))[-1]
    assert (last["type"], last["resource_type"], last["resource_version"]) == (
        "document_content_updated", "document", 2,
    )  # fmt: skip
    assert last["payload"] == {"document_id": doc["id"]} and "Hello" not in str(last)
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE room_seq = :s", s=last["seq"])
        == 1
    )
    reread = (await alice.get(f"/rooms/{room_id}/documents/{doc['id']}")).json()
    assert reread["content"] == body and reread["content_version"] == 2


async def test_versions_advance_one_at_a_time_across_saves(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    for expected in range(1, 6):
        response = await save(alice, room_id, doc["id"], expected, content(f"v{expected}"))
        assert response.json()["content_version"] == expected + 1


# ---- conflicts and no-ops -----------------------------------------------------------------------------------


async def test_a_stale_version_conflicts_even_when_the_content_is_identical(
    alice, bob, sessionmaker
):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"))
    body = content("same")
    assert (await save(alice, room_id, doc["id"], 1, body)).status_code == 200
    events = await events_of(sessionmaker, room_id)
    response = await save(bob, room_id, doc["id"], 1, body)  # bob's version 1 is stale
    error = response.json()["error"]
    assert (response.status_code, error["code"], error["current_content_version"]) == (
        409, "document_version_conflict", 2,
    )  # fmt: skip
    assert await events_of(sessionmaker, room_id) == events
    assert (await stored(sessionmaker, doc["id"]))["content_version"] == 2


async def test_a_future_version_is_also_a_conflict(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    response = await save(alice, room_id, doc["id"], 5, content("x"))
    assert response.status_code == 409 and response.json()["error"]["current_content_version"] == 1


async def test_unchanged_content_is_not_a_new_version_event_or_rate_limit_use(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    first = (await save(alice, room_id, doc["id"], 1, doc["content"])).json()
    # the template content is what is stored, so saving it again at version 1 changes nothing
    assert first == {
        "content_version": 1, "content_hash": doc["content_hash"], "canonical_changed": False,
        "unchanged": True, "content": None,
    }  # fmt: skip
    assert await bucket_total(sessionmaker) == 0
    assert (
        len(await events_of(sessionmaker, room_id)) == 3
    )  # room_created + the two creation events
    again = (await save(alice, room_id, doc["id"], 1, doc["content"])).json()
    assert (
        again["unchanged"] is True
        and (await stored(sessionmaker, doc["id"]))["content_version"] == 1
    )


async def test_exactly_one_of_two_concurrent_saves_wins(alice, bob, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"))
    responses = await asyncio.gather(
        save(alice, room_id, doc["id"], 1, content("alice")),
        save(bob, room_id, doc["id"], 1, content("bob")),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r for r in responses if r.status_code == 200)
    row = await stored(sessionmaker, doc["id"])
    assert row["content_version"] == 2 == winner.json()["content_version"]
    assert [e["type"] for e in await events_of(sessionmaker, room_id, after=3)] == [
        "document_content_updated"
    ]
    assert row["content"]["blocks"][0]["children"][0]["text"] in ("alice", "bob")


async def test_many_concurrent_saves_still_have_exactly_one_winner(alice, bob, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"))
    responses = await asyncio.gather(
        *[save(alice if i % 2 else bob, room_id, doc["id"], 1, content(f"t{i}")) for i in range(8)]
    )
    assert [r.status_code for r in responses].count(200) == 1
    assert [r.status_code for r in responses].count(409) == 7


# ---- validation ---------------------------------------------------------------------------------------------


async def test_invalid_content_is_422_with_a_field_path_and_changes_nothing(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    bad = content("x")
    bad["blocks"][0]["children"][0]["marks"] = {"link": {"href": "javascript:alert(1)"}}
    before = (await stored(sessionmaker, doc["id"]), await events_of(sessionmaker, room_id))
    response = await save(alice, room_id, doc["id"], 1, bad)
    error = response.json()["error"]
    assert (response.status_code, error["code"]) == (422, "invalid_document_content")
    assert error["errors"] == [
        {
            "path": "/blocks/0/children/0/marks/link/href",
            "code": "invalid_link",
            "message": "only http, https and mailto links are allowed",
        }
    ]
    assert "javascript" not in response.text  # the submitted value is never echoed
    assert (await stored(sessionmaker, doc["id"]), await events_of(sessionmaker, room_id)) == before
    assert await bucket_total(sessionmaker) == 0


@pytest.mark.parametrize(
    ("mutate", "path", "code"),
    [
        (lambda c: c["blocks"].append(dict(c["blocks"][0])), "/blocks/1/id", "duplicate_id"),
        (lambda c: c["blocks"][0].update(id="nope"), "/blocks/0/id", "invalid_id"),
        (lambda c: c["blocks"].clear(), "/blocks", "invalid_structure"),
        (lambda c: c.update(extra=1), "/extra", "unknown_field"),
        (lambda c: c["blocks"][0].update(type="div"), "/blocks/0/type", "invalid_structure"),
    ],
)
async def test_structural_errors_are_reported_with_paths(alice, sessionmaker, mutate, path, code):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    body = content("x")
    mutate(body)
    error = (await save(alice, room_id, doc["id"], 1, body)).json()["error"]
    assert (error["errors"][0]["path"], error["errors"][0]["code"]) == (path, code)


async def test_a_wrong_schema_version_is_rejected(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    response = await alice.put(
        url(room_id, doc["id"]),
        json={"schema_version": 2, "expected_content_version": 1, "content": content("x")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["errors"][0]["path"] == "/schema_version"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"schema_version": 1, "expected_content_version": 0, "content": {}},
        {"schema_version": 1, "expected_content_version": "1", "content": {}},
        {"schema_version": 1, "expected_content_version": 1},
        {"schema_version": 1, "expected_content_version": 1, "content": [], "x": 1},
        {"schema_version": 1, "expected_content_version": 2**63, "content": {}},
    ],
)
async def test_malformed_save_bodies_are_422(alice, sessionmaker, bad):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    response = await alice.put(url(room_id, doc["id"]), json=bad)
    assert response.status_code == 422


async def test_configured_limits_apply_with_a_path_and_nothing_is_truncated(
    alice, make_actor, sessionmaker, settings
):
    cfg = settings.model_copy(update={"document_max_text_codepoints": 10})
    owner = await make_actor("Limit", app_settings=cfg)
    room_id = await new_room(owner)
    doc = (await create_doc(owner, room_id)).json()
    response = await save(owner, room_id, doc["id"], 1, content("x" * 11))
    error = response.json()["error"]["errors"][0]
    assert (response.status_code, error["path"], error["code"]) == (
        422, "/blocks/0/children/0/text", "limit_exceeded",
    )  # fmt: skip
    assert (await save(owner, room_id, doc["id"], 1, content("x" * 10))).status_code == 200


async def test_a_document_over_the_serialized_limit_is_422_not_413(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    big = {
        "schema_version": 1,
        "blocks": [
            paragraph(
                "x" * 20,
                align="center",
                line_spacing=2,
                space_after_pt=72,
                space_before_pt=72,
                indent_level=8,
            )  # fmt: skip
            for _ in range(4900)
        ],
    }
    assert len(json.dumps(big)) < 2 * 1024 * 1024
    response = await save(alice, room_id, doc["id"], 1, big)
    error = response.json()["error"]["errors"][0]
    assert (response.status_code, error["path"], error["code"]) == (422, "", "limit_exceeded")


# ---- canonicalization -----------------------------------------------------------------------------------------


async def test_canonical_changed_returns_the_canonical_content_and_stores_it(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    pid = new_id()
    sent = {
        "schema_version": 1,
        "blocks": [
            {
                "type": "paragraph",
                "id": pid.upper(),
                "children": [
                    {"type": "text", "text": "a", "marks": {"bold": False, "color": "#aabbcc"}},
                    {"type": "text", "text": ""},
                    {"type": "text", "text": "b", "marks": {"color": "#AABBCC"}},
                ],
            }
        ],
    }
    out = (await save(alice, room_id, doc["id"], 1, sent)).json()
    canonical = {
        "schema_version": 1,
        "blocks": [
            {
                "type": "paragraph",
                "id": pid,
                "children": [{"type": "text", "text": "ab", "marks": {"color": "#AABBCC"}}],
            }
        ],
    }
    assert out["canonical_changed"] is True and out["content"] == canonical
    assert (await stored(sessionmaker, doc["id"]))["content"] == canonical


async def test_client_produced_canonical_content_never_reports_a_change(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker, template="technical_proposal")
    fetched = (await alice.get(f"/rooms/{room_id}/documents/{doc['id']}")).json()["content"]
    edited = json.loads(json.dumps(fetched))
    edited["blocks"][1]["children"] = [{"type": "text", "text": "Because."}]
    edited["blocks"][0]["attrs"] = {"align": "center", "line_spacing": 2.0, "space_after_pt": 12.0}
    out = (await save(alice, room_id, doc["id"], 1, edited)).json()
    assert (out["canonical_changed"], out["content"]) == (False, None)  # 2.0 and 2 are the same


# ---- atomicity ---------------------------------------------------------------------------------------------------


async def test_a_failure_after_the_write_rolls_back_everything(alice, sessionmaker, monkeypatch):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    asset = await insert_asset(sessionmaker, room_id, alice.id)
    from app.services import documents as service

    real = service.record_room_event

    async def record_then_fail(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("boom after the event insert")

    monkeypatch.setattr(service, "record_room_event", record_then_fail)
    before = (await stored(sessionmaker, doc["id"]), await events_of(sessionmaker, room_id))
    response = await save(alice, room_id, doc["id"], 1, with_image(asset))
    assert response.status_code == 500
    assert (await stored(sessionmaker, doc["id"]), await events_of(sessionmaker, room_id)) == before
    assert await bucket_total(sessionmaker) == 0  # the failed save consumed no rate-limit capacity
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_asset_refs") == 0
    assert await scalar(sessionmaker, "SELECT state FROM room_assets") == "pending"
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox") == 3
    monkeypatch.undo()
    assert (await save(alice, room_id, doc["id"], 1, content("works now"))).status_code == 200


# ---- access races ---------------------------------------------------------------------------------------------------


async def _race_membership_change(sessionmaker, room_id, user_id, new_state_sql, saver_call):
    """Hold the room lock while changing membership, start a save that must wait for it, commit."""
    async with sessionmaker() as db, db.begin():
        await db.execute(
            text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room_id}
        )
        await db.execute(
            text(f"UPDATE memberships SET {new_state_sql} WHERE room_id = :r AND user_id = :u"),
            {"r": room_id, "u": user_id},
        )
        task = asyncio.create_task(saver_call())
        await asyncio.sleep(0.4)
        assert (
            not task.done()
        )  # blocked on the room lock, with a stale "editor" pre-check behind it
    return await task


async def test_a_downgrade_that_commits_first_wins_over_a_waiting_save(alice, bob, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"))
    response = await _race_membership_change(
        sessionmaker, room_id, bob.id, "role = 'viewer'",
        lambda: save(bob, room_id, doc["id"], 1, content("late")),
    )  # fmt: skip
    assert (response.status_code, response.json()["error"]["code"]) == (403, "insufficient_role")
    assert (await stored(sessionmaker, doc["id"]))["content_version"] == 1


async def test_a_removal_that_commits_first_wins_over_a_waiting_save(alice, bob, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"))
    response = await _race_membership_change(
        sessionmaker, room_id, bob.id, "state = 'removed', removed_at = now()",
        lambda: save(bob, room_id, doc["id"], 1, content("late")),
    )  # fmt: skip
    assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    assert (await stored(sessionmaker, doc["id"]))["content_version"] == 1


async def test_a_save_that_commits_first_survives_a_later_downgrade(alice, bob, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker, (bob, "editor"))
    assert (await save(bob, room_id, doc["id"], 1, content("first"))).status_code == 200
    await execute(
        sessionmaker, "UPDATE memberships SET role = 'viewer' WHERE room_id = :r AND user_id = :u",
        r=room_id, u=bob.id,
    )  # fmt: skip
    assert (await save(bob, room_id, doc["id"], 2, content("second"))).status_code == 403
    assert (await stored(sessionmaker, doc["id"]))["content"]["blocks"][0]["children"][0][
        "text"
    ] == "first"


# ---- rate limits -----------------------------------------------------------------------------------------------------


async def test_the_document_write_limit_returns_429_and_rejected_writes_consume_nothing(
    make_actor, sessionmaker, settings
):
    cfg = settings.model_copy(update={"document_writes_per_minute": 3})
    owner = await make_actor("Limited", app_settings=cfg)
    room_id = await new_room(owner)
    doc = (await create_doc(owner, room_id)).json()
    for version in (1, 2, 3):
        assert (
            await save(owner, room_id, doc["id"], version, content(f"v{version}"))
        ).status_code == 200
    refused = await save(owner, room_id, doc["id"], 4, content("v4"))
    assert (refused.status_code, refused.json()["error"]["code"]) == (429, "rate_limited")
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert await bucket_total(sessionmaker) == 3  # the refusal added nothing
    assert (await stored(sessionmaker, doc["id"]))["content_version"] == 4
    # a stale or unchanged save never needs capacity, so it answers normally while limited
    stale = await save(owner, room_id, doc["id"], 1, content("old"))
    assert stale.status_code == 409
    same = await save(
        owner, room_id, doc["id"], 4, content("v3")
    )  # different ids => changed => 429
    assert same.status_code == 429


async def test_document_saves_and_chat_have_separate_quotas(make_actor, sessionmaker, settings):
    cfg = settings.model_copy(
        update={"document_writes_per_minute": 2, "message_writes_per_minute": 2}
    )
    owner = await make_actor("Both", app_settings=cfg)
    room_id = await new_room(owner)
    doc = (await create_doc(owner, room_id)).json()
    assert (await save(owner, room_id, doc["id"], 1, content("1"))).status_code == 200
    assert (await save(owner, room_id, doc["id"], 2, content("2"))).status_code == 200
    assert (await save(owner, room_id, doc["id"], 3, content("3"))).status_code == 429
    # chat is untouched by the exhausted document quota...
    assert (await send(owner, room_id, "one")).status_code == 201
    assert (await send(owner, room_id, "two")).status_code == 201
    assert (await send(owner, room_id, "three")).status_code == 429
    # ...and the exhausted chat quota did not consume anything from the document table
    assert await bucket_total(sessionmaker) == 2
    assert await scalar(sessionmaker, "SELECT sum(count) FROM message_write_buckets") == 2


async def test_each_user_has_their_own_document_bucket(make_actor, sessionmaker, settings):
    cfg = settings.model_copy(update={"document_writes_per_minute": 1})
    owner = await make_actor("O", app_settings=cfg)
    editor = await make_actor("E", app_settings=cfg)
    room_id = await new_room(owner)
    await add_member(sessionmaker, room_id, editor.id, "editor")
    doc = (await create_doc(owner, room_id)).json()
    assert (await save(owner, room_id, doc["id"], 1, content("o"))).status_code == 200
    assert (await save(editor, room_id, doc["id"], 2, content("e"))).status_code == 200


# ---- request size limits ---------------------------------------------------------------------------------------------


def _raw_headers(csrf):
    return {"X-CSRF-Token": csrf, "Origin": TRUSTED_ORIGIN, "Content-Type": "application/json"}


async def test_a_content_save_may_be_up_to_2_mib_but_not_more(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    csrf = await alice.csrf_token()
    pad = b"x" * (1_500_000)  # over the old 128 KiB limit, under 2 MiB: reaches the validator
    under = b'{"schema_version":1,"expected_content_version":1,"content":{"pad":"%s"}}' % pad
    response = await alice.client.put(
        url(room_id, doc["id"]), content=under, headers=_raw_headers(csrf)
    )
    assert response.status_code == 422  # unknown_field "pad": it was read and validated
    over = b'{"schema_version":1,"expected_content_version":1,"content":{"pad":"%s"}}' % (
        b"x" * 2_200_000
    )
    response = await alice.client.put(
        url(room_id, doc["id"]), content=over, headers=_raw_headers(csrf)
    )
    assert (response.status_code, response.json()["error"]["code"]) == (413, "payload_too_large")


async def test_a_lying_content_length_cannot_sneak_past_the_save_limit(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    csrf = await alice.csrf_token()
    over = b'{"schema_version":1,"expected_content_version":1,"content":{"pad":"%s"}}' % (
        b"x" * 2_200_000
    )
    response = await alice.client.put(
        url(room_id, doc["id"]),
        content=over,
        headers={**_raw_headers(csrf), "Content-Length": "100"},
    )
    assert response.status_code == 413


async def test_other_document_routes_keep_the_128_kib_limit(alice, sessionmaker):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    csrf = await alice.csrf_token()
    big = b'{"title":"%s"}' % (b"x" * 140_000)
    for method, path in (
        ("PATCH", f"/rooms/{room_id}/documents/{doc['id']}"),
        ("POST", f"/rooms/{room_id}/documents"),
        ("PUT", f"/rooms/{room_id}/active-document"),
    ):
        response = await alice.client.request(method, path, content=big, headers=_raw_headers(csrf))
        assert response.status_code == 413, (method, path)


# ---- asset references (the pipeline that creates assets arrives with the image checkpoint) ---------------------------------------


async def test_a_save_may_only_reference_existing_assets_of_this_room(alice, bob, sessionmaker):
    room_a, doc_a = await room_with_doc(alice, sessionmaker)
    room_b = await new_room(bob)
    foreign = await insert_asset(sessionmaker, room_b, bob.id)
    for bad in (new_id(), foreign):
        response = await save(alice, room_a, doc_a["id"], 1, with_image(bad))
        error = response.json()["error"]
        assert (response.status_code, error["code"], error["asset_ids"]) == (
            422,
            "asset_not_found",
            [bad],
        )
    expired = await insert_asset(sessionmaker, room_a, alice.id, state="expired")
    assert (await save(alice, room_a, doc_a["id"], 1, with_image(expired))).status_code == 422
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_asset_refs") == 0
    assert (await stored(sessionmaker, doc_a["id"]))["content_version"] == 1


async def test_referencing_an_asset_attaches_it_and_dropping_it_marks_it_unreferenced(
    alice, sessionmaker
):
    room_id, doc = await room_with_doc(alice, sessionmaker)
    asset = await insert_asset(sessionmaker, room_id, alice.id)
    assert (await save(alice, room_id, doc["id"], 1, with_image(asset))).status_code == 200
    [row] = await rows(
        sessionmaker, "SELECT state, attached_at, unreferenced_since FROM room_assets"
    )
    assert row["state"] == "attached" and row["attached_at"] and row["unreferenced_since"] is None
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_asset_refs") == 1
    assert (await save(alice, room_id, doc["id"], 2, content("no image"))).status_code == 200
    [row] = await rows(sessionmaker, "SELECT state, unreferenced_since FROM room_assets")
    assert row["state"] == "attached" and row["unreferenced_since"] is not None  # NOT deleted
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_asset_refs") == 0
    # an undo within the grace period: referencing it again clears unreferenced_since
    assert (await save(alice, room_id, doc["id"], 3, with_image(asset))).status_code == 200
    assert await scalar(sessionmaker, "SELECT unreferenced_since FROM room_assets") is None


async def test_the_database_itself_refuses_a_cross_room_asset_reference(alice, bob, sessionmaker):
    room_a, doc_a = await room_with_doc(alice, sessionmaker)
    room_b = await new_room(bob)
    foreign = await insert_asset(sessionmaker, room_b, bob.id)
    with pytest.raises(Exception, match="fk_document_asset_refs_room_asset"):
        await execute(
            sessionmaker,
            "INSERT INTO document_asset_refs (document_id, asset_id, room_id) VALUES (:d, :a, :r)",
            d=doc_a["id"], a=foreign, r=room_a,
        )  # fmt: skip
