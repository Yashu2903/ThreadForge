"""POST /rooms/{id}/messages: permissions, validation, storage, size limit, duplicate protection."""

import asyncio
import hashlib
import logging
import uuid

import pytest

from app.services import messages as messages_service
from tests.factories import TEST_FINGERPRINT_SECRET, make_settings
from tests.helpers import TRUSTED_ORIGIN, add_member, execute, rows, scalar
from tests.test_invitations_api import new_room

MESSAGE_FIELDS = {
    "id", "room_id", "author_id", "author_display_name", "client_message_id", "body", "kind", "ai_request_id",
    "created_at", "edited_at", "deleted_at", "deleted_by", "version", "is_deleted",
}  # fmt: skip


async def send(actor, room_id, body="hello", cmid=None, **kw):
    cmid = cmid or str(uuid.uuid4())
    return await actor.post(
        f"/rooms/{room_id}/messages", json={"client_message_id": cmid, "body": body}, **kw
    )


async def raw_send(actor, room_id, raw: bytes, **kw):
    return await actor.post(
        f"/rooms/{room_id}/messages",
        content=raw,
        headers={"Content-Type": "application/json"},
        **kw,
    )


async def message_rows(sm):
    return await rows(sm, "SELECT * FROM messages ORDER BY created_at, id")


async def editor_room(alice, bob, sm):
    room_id = await new_room(alice)
    await add_member(sm, room_id, bob.id, "editor")
    return room_id


# ---- permissions ---------------------------------------------------------------------------------


async def test_owner_and_editor_can_send_and_the_server_sets_author_and_time(
    alice, bob, sessionmaker
):
    room_id = await editor_room(alice, bob, sessionmaker)
    for actor, name in ((alice, "Alice"), (bob, "Bob")):
        response = await send(actor, room_id, f"hi from {name}")
        assert response.status_code == 201
        body = response.json()
        assert set(body) == MESSAGE_FIELDS
        assert (body["author_id"], body["author_display_name"]) == (str(actor.id), name)
        assert (body["version"], body["is_deleted"], body["edited_at"], body["deleted_at"]) == (
            1,
            False,
            None,
            None,
        )
        assert response.headers["cache-control"] == "no-store"
    stored = await scalar(
        sessionmaker,
        "SELECT count(*) FROM messages WHERE created_at BETWEEN now() - interval '1 minute' AND now() + interval '1 minute'",
    )
    assert stored == 2  # timestamps are the database's


async def test_message_is_visible_from_another_database_session(alice, sessionmaker):
    room_id = await new_room(alice)
    sent = (await send(alice, room_id, "persisted")).json()
    [row] = await message_rows(sessionmaker)  # a different session than the app's
    assert (
        str(row["id"]) == sent["id"]
        and row["body"] == "persisted"
        and row["room_id"] == uuid.UUID(room_id)
    )


async def test_every_active_member_can_send_whatever_their_role(alice, bob, carol, sessionmaker):
    """Chat depends only on active membership: owner, editor and viewer all get 201, and a
    matching retry gets 200 exactly as it does for an editor."""
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await add_member(sessionmaker, room_id, carol.id, "viewer")
    for actor, role in ((alice, "owner"), (bob, "editor"), (carol, "viewer")):
        cmid = str(uuid.uuid4())
        created = await send(actor, room_id, f"hello from the {role}", cmid)
        assert created.status_code == 201, role
        assert created.json()["author_id"] == str(actor.id)
        retry = await send(actor, room_id, f"hello from the {role}", cmid)
        assert (retry.status_code, retry.json()["id"]) == (200, created.json()["id"]), role
    assert len(await message_rows(sessionmaker)) == 3


async def test_removed_member_nonmember_and_anonymous_cannot_send(
    alice, bob, carol, make_actor, client, sessionmaker
):
    room_id = await new_room(alice)
    removed = await make_actor("Removed")
    await add_member(sessionmaker, room_id, removed.id, "editor", "removed")
    removed_viewer = await make_actor("RemovedViewer")
    await add_member(sessionmaker, room_id, removed_viewer.id, "viewer", "removed")

    # a removed member of ANY role, and a user who was never a member: 404, no role code at all
    for actor in (removed, removed_viewer, carol):
        response = await send(actor, room_id)
        assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    anonymous = await client.post(
        f"/rooms/{room_id}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": "x"},
    )
    assert (anonymous.status_code, anonymous.json()["error"]["code"]) == (401, "unauthenticated")
    assert await message_rows(sessionmaker) == []


async def test_gate_order_is_auth_then_csrf_then_access_then_validation(
    alice, bob, carol, client, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    path = f"/rooms/{room_id}/messages"
    junk = b"{this is not json"
    headers = {"Content-Type": "application/json"}

    assert (await client.post(path, content=junk, headers=headers)).status_code == 401
    no_csrf = await alice.post(path, content=junk, headers=headers, csrf=False)
    assert (no_csrf.status_code, no_csrf.json()["error"]["code"]) == (403, "csrf_failed")
    assert (await carol.post(path, content=junk, headers=headers)).status_code == 404  # nonmember
    # any ACTIVE member, viewer included, passes the access gate and reaches validation
    assert (await bob.post(path, content=junk, headers=headers)).status_code == 422  # viewer
    assert (await alice.post(path, content=junk, headers=headers)).status_code == 422  # owner


async def test_csrf_is_required(alice, sessionmaker):
    room_id = await new_room(alice)
    for kwargs in ({"csrf": False}, {"origin": False}):
        response = await send(alice, room_id, **kwargs)
        assert (response.status_code, response.json()["error"]["code"]) == (403, "csrf_failed")
    assert await message_rows(sessionmaker) == []


# ---- validation ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"author_id": str(uuid.uuid4())},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"role": "owner"},
        {"room_id": str(uuid.uuid4())},
        {"version": 7},
        {"id": str(uuid.uuid4())},
        {"deleted_at": None},
        {"expected_version": 1},
    ],
)
async def test_unexpected_fields_are_rejected(alice, sessionmaker, extra):
    room_id = await new_room(alice)
    payload = {"client_message_id": str(uuid.uuid4()), "body": "x", **extra}
    response = await alice.post(f"/rooms/{room_id}/messages", json=payload)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")
    assert await message_rows(sessionmaker) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"body": "x"},
        {"client_message_id": "00000000-0000-4000-8000-000000000001"},
        {"client_message_id": "not-a-uuid", "body": "x"},
        {"client_message_id": 12345, "body": "x"},
        {"client_message_id": None, "body": "x"},
        {"client_message_id": "00000000000040008000000000000001", "body": "x"},  # no hyphens
        {"client_message_id": "{00000000-0000-4000-8000-000000000001}", "body": "x"},
        {"client_message_id": "00000000-0000-4000-8000-000000000001", "body": 5},
        {"client_message_id": "00000000-0000-4000-8000-000000000001", "body": None},
        {"client_message_id": "00000000-0000-4000-8000-000000000001", "body": ["x"]},
        {"client_message_id": "00000000-0000-4000-8000-000000000001", "body": True},
    ],
)
async def test_structurally_invalid_requests_are_422_without_a_row(alice, sessionmaker, payload):
    room_id = await new_room(alice)
    response = await alice.post(f"/rooms/{room_id}/messages", json=payload)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")
    assert await message_rows(sessionmaker) == []


@pytest.mark.parametrize("raw", [b"", b"null", b"[]", b'"text"', b"123", b"{bad", b"\xff\xfe\x00"])
async def test_non_object_or_malformed_json_is_422(alice, raw):
    room_id = await new_room(alice)
    response = await raw_send(alice, room_id, raw)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")


CMID = "00000000-0000-4000-8000-0000000000aa"


@pytest.mark.parametrize(
    "body_json",
    [
        '""',  # empty
        '"   "',  # spaces only
        '" \\t\\r\\n "',  # whitespace only
        '"\\u200b"',  # zero-width space (Cf)
        '"\\u200b\\u200d\\u2060\\ufeff"',  # only format characters
        '"\\u200b \\n \\u200e"',  # format characters mixed with whitespace
        '"a\\u0000b"',  # NUL
        '"\\ud800"',  # lone high surrogate
        '"abc\\udc00def"',  # lone low surrogate
        '"\\ud83d"',  # high surrogate without its pair
        '"bell\\u0007"',
        '"esc\\u001b[31m"',
        '"del\\u007f"',
        '"c1\\u0085control"',
        '"vt\\u000bform\\u000c"',
    ],
)
async def test_forbidden_bodies_are_422_never_500(alice, sessionmaker, body_json):
    room_id = await new_room(alice)
    raw = f'{{"client_message_id": "{CMID}", "body": {body_json}}}'.encode("ascii")
    response = await raw_send(alice, room_id, raw)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")
    assert await message_rows(sessionmaker) == []


async def test_length_limits_count_code_points(alice, sessionmaker):
    room_id = await new_room(alice)
    assert (await send(alice, room_id, "x" * 5001)).status_code == 422
    assert (await send(alice, room_id, "\U0001f600" * 5001)).status_code == 422  # emoji = 1 point
    assert (await send(alice, room_id, "x" * 5000)).status_code == 201
    emoji = await send(alice, room_id, "\U0001f600" * 5000)  # 20,000 bytes in UTF-8
    assert emoji.status_code == 201
    accented = await send(alice, room_id, "é" * 5000)
    assert accented.status_code == 201
    stored = await scalar(sessionmaker, "SELECT max(char_length(body)) FROM messages")
    assert stored == 5000


async def test_body_is_stored_exactly_with_no_normalization(alice, sessionmaker):
    room_id = await new_room(alice)
    bodies = [
        "  leading and trailing  \r\n\tlines\n\n  ",  # whitespace and line breaks preserved
        "é",  # decomposed (NFD): must NOT become the precomposed character
        "ﬁ ligature",  # compatibility character: must NOT be folded to "fi"
        "tab\tseparated\r\nwindows\nunix\rmac",
        "a​b",  # a zero-width char inside visible text is kept
        "<script>alert(1)</script> & &amp;",  # plain text: never interpreted
    ]
    ids = []
    for text in bodies:
        response = await send(alice, room_id, text)
        assert response.status_code == 201 and response.json()["body"] == text
        ids.append(response.json()["id"])
    stored = {str(r["id"]): r["body"] for r in await message_rows(sessionmaker)}
    assert [stored[i] for i in ids] == bodies


async def test_validation_errors_and_logs_never_contain_the_body(alice, caplog):
    caplog.set_level(logging.DEBUG)
    room_id = await new_room(alice)
    marker = "TOP-SECRET-MARKER-9f3a"
    bad = await alice.post(
        f"/rooms/{room_id}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": marker + "\x00", "author_id": "x"},
    )
    assert bad.status_code == 422 and marker not in bad.text
    good = await send(alice, room_id, marker + " fine")
    assert good.status_code == 201
    logged = "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records)
    assert logged and marker not in logged
    assert "action=message_sent" in logged  # structured line exists, content-free


# ---- request size limit (413) ---------------------------------------------------------------------


def _big(n=200_000):
    return b'{"client_message_id":"%s","body":"%s"}' % (CMID.encode(), b"x" * n)


async def test_oversized_body_is_413_even_before_authentication(client, alice):
    room_id = await new_room(alice)
    anonymous = await client.post(
        f"/rooms/{room_id}/messages", content=_big(), headers={"Content-Type": "application/json"}
    )
    assert anonymous.status_code == 413  # the size gate is first: not 401
    error = anonymous.json()["error"]
    assert (
        error["code"] == "payload_too_large"
        and error["request_id"] == anonymous.headers["x-request-id"]
    )
    assert anonymous.headers["cache-control"] == "no-store"


async def test_oversized_body_is_413_with_a_missing_content_length(alice, sessionmaker):
    room_id = await new_room(alice)

    async def chunks():  # no Content-Length: chunked transfer
        for _ in range(40):
            yield b"x" * 5000

    csrf = await alice.csrf_token()
    response = await alice.client.post(
        f"/rooms/{room_id}/messages",
        content=chunks(),
        headers={
            "X-CSRF-Token": csrf,
            "Origin": TRUSTED_ORIGIN,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413
    assert await message_rows(sessionmaker) == []


async def test_oversized_body_is_413_with_a_false_content_length(alice):
    room_id = await new_room(alice)
    csrf = await alice.csrf_token()
    response = await alice.client.post(
        f"/rooms/{room_id}/messages",
        content=_big(),
        headers={
            "X-CSRF-Token": csrf,
            "Origin": TRUSTED_ORIGIN,
            "Content-Type": "application/json",
            "Content-Length": "10",  # a lie: the body is ~200 KB
        },
    )
    assert response.status_code == 413  # counted as it streamed, header not trusted


async def test_body_just_under_the_limit_is_processed_not_rejected(alice):
    room_id = await new_room(alice)
    raw = b'{"client_message_id":"%s","body":"%s"}' % (CMID.encode(), b"x" * 100_000)
    response = await raw_send(alice, room_id, raw)
    assert response.status_code == 422  # reached validation (body > 5000 chars), not the size gate


# ---- duplicate-send protection ---------------------------------------------------------------------


async def test_same_id_and_body_returns_the_same_message_201_then_200(alice, sessionmaker):
    room_id = await new_room(alice)
    cmid = str(uuid.uuid4())
    first = await send(alice, room_id, "once", cmid)
    retry = await send(alice, room_id, "once", cmid)
    assert (first.status_code, retry.status_code) == (201, 200)
    assert first.json()["id"] == retry.json()["id"]
    assert retry.json() == first.json()
    assert len(await message_rows(sessionmaker)) == 1


async def test_client_message_id_case_is_normalised(alice, sessionmaker):
    room_id = await new_room(alice)
    cmid = str(uuid.uuid4())
    assert (await send(alice, room_id, "same", cmid.lower())).status_code == 201
    assert (await send(alice, room_id, "same", cmid.upper())).status_code == 200
    assert len(await message_rows(sessionmaker)) == 1


async def test_same_text_with_a_new_id_is_a_second_message(alice, sessionmaker):
    room_id = await new_room(alice)
    a = await send(alice, room_id, "repeat")
    b = await send(alice, room_id, "repeat")
    assert (a.status_code, b.status_code) == (201, 201) and a.json()["id"] != b.json()["id"]
    assert len(await message_rows(sessionmaker)) == 2


async def test_same_id_with_a_different_body_is_message_id_reused(alice, sessionmaker):
    room_id = await new_room(alice)
    cmid = str(uuid.uuid4())
    await send(alice, room_id, "ORIGINAL-TEXT-7731", cmid)
    response = await send(alice, room_id, "ALTERED-TEXT-7731", cmid)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "message_id_reused")
    [row] = await message_rows(sessionmaker)
    assert row["body"] == "ORIGINAL-TEXT-7731" and row["version"] == 1
    assert "TEXT-7731" not in response.text  # neither version of the text is echoed back


async def test_the_same_client_id_is_independent_across_authors_and_rooms(alice, bob, sessionmaker):
    room_a = await new_room(alice)
    room_b = await new_room(alice, "other")
    await add_member(sessionmaker, room_a, bob.id, "editor")
    cmid = str(uuid.uuid4())
    results = [
        await send(alice, room_a, "x", cmid),
        await send(bob, room_a, "x", cmid),
        await send(alice, room_b, "x", cmid),
    ]
    assert [r.status_code for r in results] == [201, 201, 201]
    assert len({r.json()["id"] for r in results}) == 3


async def test_a_retry_is_unaffected_by_role_changes_but_a_removed_user_gets_nothing(
    alice, bob, sessionmaker
):
    room_id = await editor_room(alice, bob, sessionmaker)
    cmid = str(uuid.uuid4())
    first = await send(bob, room_id, "mine", cmid)
    assert first.status_code == 201
    await add_member(sessionmaker, room_id, bob.id, "viewer")  # downgraded: no effect on chat
    retry = await send(bob, room_id, "mine", cmid)
    assert (retry.status_code, retry.json()["id"]) == (200, first.json()["id"])
    await add_member(sessionmaker, room_id, bob.id, "editor")  # and back up again
    assert (await send(bob, room_id, "mine", cmid)).json()["id"] == first.json()["id"]
    await add_member(sessionmaker, room_id, bob.id, "editor", "removed")  # removal DOES block
    response = await send(bob, room_id, "mine", cmid)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    assert "mine" not in response.text and first.json()["id"] not in response.text  # no data


async def test_a_matching_retry_writes_nothing(alice, sessionmaker):
    room_id = await new_room(alice)
    cmid = str(uuid.uuid4())
    await send(alice, room_id, "once", cmid)
    before = await message_rows(sessionmaker)
    buckets = await rows(sessionmaker, "SELECT * FROM message_write_buckets")
    for _ in range(3):
        assert (await send(alice, room_id, "once", cmid)).status_code == 200
    assert await message_rows(sessionmaker) == before
    assert (
        await rows(sessionmaker, "SELECT * FROM message_write_buckets") == buckets
    )  # no capacity used


async def test_simultaneous_identical_sends_create_one_row(alice, sessionmaker):
    room_id = await new_room(alice)
    cmid = str(uuid.uuid4())
    results = await asyncio.gather(*[send(alice, room_id, "double click", cmid) for _ in range(6)])
    assert sorted(r.status_code for r in results) == [200] * 5 + [201]
    assert len({r.json()["id"] for r in results}) == 1
    assert len(await message_rows(sessionmaker)) == 1


async def test_the_unique_violation_recovery_path_answers_as_a_retry_not_a_500(
    alice, sessionmaker, settings, monkeypatch, caplog
):
    """Without the room lock the constraint is the only safeguard. Two identical sends are forced
    (with a barrier) to BOTH pass the lookup before either inserts; the loser's INSERT raises the
    unique violation, its transaction rolls back, and it re-runs and answers as a retry."""
    caplog.set_level(logging.INFO, logger="threadforge")
    room_id = await new_room(alice)
    cmid = uuid.uuid4()

    async def unlocked_authorize(db, room, actor):  # membership check WITHOUT the room lock
        from sqlalchemy import text

        from app.services.room_lock import adopt_new_room

        role = await scalar_role(db, room, actor)
        row = (await db.execute(text("SELECT * FROM rooms WHERE id = :r"), {"r": room})).one()
        # Deliberately NOT holding the lock (this test exercises the unique-constraint safeguard).
        # Only the winning transaction ever reaches record_room_event, so no sequence race here.
        return adopt_new_room(row), role

    async def scalar_role(db, room, actor):
        from sqlalchemy import text

        return (
            await db.execute(
                text(
                    "SELECT role FROM memberships WHERE room_id = :r AND user_id = :u AND state = 'active'"
                ),
                {"r": room, "u": actor},
            )
        ).scalar_one()

    barrier = asyncio.Barrier(2)
    real_consume = messages_service._consume_write_capacity

    async def after_lookup_before_insert(db, settings_, room, user):
        await asyncio.wait_for(barrier.wait(), timeout=5)  # both have looked up: nothing found
        await real_consume(db, settings_, room, user)

    monkeypatch.setattr(messages_service, "_authorize_member", unlocked_authorize)
    monkeypatch.setattr(messages_service, "_consume_write_capacity", after_lookup_before_insert)

    results = await asyncio.gather(
        *[
            messages_service.send_message(
                sessionmaker, settings, alice.id, uuid.UUID(room_id), cmid, "raced"
            )
            for _ in range(2)
        ]
    )
    assert sorted(r.created for r in results) == [False, True]  # one insert, one retry answer
    assert results[0].message.id == results[1].message.id
    assert "duplicate send raced past the lookup" in caplog.text  # the recovery path really ran
    assert len(await message_rows(sessionmaker)) == 1
    assert (
        await scalar(sessionmaker, "SELECT sum(count) FROM message_write_buckets") == 1
    )  # loser rolled back


async def test_a_unique_violation_on_another_constraint_is_not_swallowed(
    alice, sessionmaker, settings
):
    """Only uq_messages_room_author_client triggers the recovery; anything else propagates."""
    from sqlalchemy.exc import IntegrityError

    room_id = await new_room(alice)
    with pytest.raises(IntegrityError):  # a 5001-char body hits the body CHECK, not the unique key
        await messages_service.send_message(
            sessionmaker, settings, alice.id, uuid.UUID(room_id), uuid.uuid4(), "x" * 5001
        )
    assert await message_rows(sessionmaker) == []


async def test_the_stored_fingerprint_is_keyed_not_a_plain_hash(alice, sessionmaker, settings):
    room_id = await new_room(alice)
    cmid = str(uuid.uuid4())
    body = "short and guessable"
    await send(alice, room_id, body, cmid)
    [row] = await message_rows(sessionmaker)
    stored = bytes(row["initial_request_fingerprint"])
    assert len(stored) == 32
    candidates = {
        hashlib.sha256(body.encode()).digest(),
        hashlib.sha256((cmid + body).encode()).digest(),
        hashlib.sha256((body + cmid).encode()).digest(),
        hashlib.sha256(f'{{"body":"{body}","client_message_id":"{cmid}"}}'.encode()).digest(),
    }
    assert stored not in candidates  # not reversible by guessing the text
    assert stored == messages_service.compute_fingerprint(settings, uuid.UUID(cmid), body)
    other = make_settings(
        message_fingerprint_secret="a-different-fingerprint-secret-0123456789abcd"
    )
    assert stored != messages_service.compute_fingerprint(other, uuid.UUID(cmid), body)
    assert TEST_FINGERPRINT_SECRET.encode() not in stored


async def test_responses_never_expose_the_fingerprint_or_credentials(alice):
    room_id = await new_room(alice)
    response = await send(alice, room_id, "x")
    text = response.text.lower()
    for forbidden in ("fingerprint", "token_hash", "secret", "email", "session"):
        assert forbidden not in text


async def test_rotating_the_secret_makes_an_inflight_retry_message_id_reused(
    alice, make_actor, sessionmaker, engine, google
):
    """Documented behaviour: after a secret rotation a retry of an earlier send fails with
    message_id_reused (acceptable: retries happen within seconds)."""
    from tests.helpers import make_client, put_cookie

    room_id = await new_room(alice)
    cmid = str(uuid.uuid4())
    assert (await send(alice, room_id, "before rotation", cmid)).status_code == 201
    rotated = make_settings(
        message_fingerprint_secret="rotated-fingerprint-secret-0123456789-abcdef"
    )
    secret = alice.client.cookies.get("tf_session")
    async with make_client(rotated, engine, google) as client:
        put_cookie(client, "tf_session", secret)
        csrf = (await client.get("/auth/csrf")).json()["csrf_token"]
        response = await client.post(
            f"/rooms/{room_id}/messages",
            json={"client_message_id": cmid, "body": "before rotation"},
            headers={"X-CSRF-Token": csrf, "Origin": TRUSTED_ORIGIN},
        )
    assert (response.status_code, response.json()["error"]["code"]) == (409, "message_id_reused")
    await execute(sessionmaker, "SELECT 1")
