"""Security checks specific to messages, and the prerequisite guarantees Part 3 relies on."""

import base64
import logging
import re
import uuid
from pathlib import Path

import pytest

from app.services import messages as messages_service
from tests.helpers import TRUSTED_ORIGIN, add_member, insert_messages, rows, scalar
from tests.test_invitations_api import new_room
from tests.test_messages_edit_delete import delete, edit, row
from tests.test_messages_send import send

# ---- CSRF on every mutation ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["no_token", "bad_token", "no_origin", "wrong_origin"])
async def test_every_message_mutation_rejects_a_failed_csrf_check(alice, sessionmaker, variant):
    room_id = await new_room(alice)
    (target,) = await insert_messages(sessionmaker, room_id, alice.id, 1)
    good = await alice.csrf_token()
    headers = {
        "no_token": {"Origin": TRUSTED_ORIGIN},
        "bad_token": {"Origin": TRUSTED_ORIGIN, "X-CSRF-Token": "0" * 64},
        "no_origin": {"X-CSRF-Token": good},
        "wrong_origin": {"Origin": "https://evil.example", "X-CSRF-Token": good},
    }[variant]
    calls = [
        alice.client.post(
            f"/rooms/{room_id}/messages",
            json={"client_message_id": str(uuid.uuid4()), "body": "x"},
            headers=headers,
        ),
        alice.client.patch(
            f"/rooms/{room_id}/messages/{target}",
            json={"body": "y", "expected_version": 1},
            headers=headers,
        ),
        alice.client.delete(
            f"/rooms/{room_id}/messages/{target}", params={"expected_version": 1}, headers=headers
        ),
    ]
    for call in calls:
        response = await call
        assert (response.status_code, response.json()["error"]["code"]) == (403, "csrf_failed")
    stored = await row(sessionmaker, target)
    assert (stored["body"], stored["version"], stored["deleted_at"]) == ("msg 1", 1, None)
    assert await scalar(sessionmaker, "SELECT count(*) FROM messages") == 1


# ---- cache headers --------------------------------------------------------------------------------------


async def test_all_message_responses_success_and_error_are_no_store(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    cmid = str(uuid.uuid4())
    created = await send(alice, room_id, "x", cmid)
    mid = created.json()["id"]
    responses = [
        created,  # 201
        await send(alice, room_id, "x", cmid),  # 200 retry
        await send(alice, room_id, "other", cmid),  # 409
        await send(bob, room_id, "viewer"),  # 403
        await send(alice, room_id, ""),  # 422
        await alice.get(f"/rooms/{room_id}/messages"),
        await alice.get(f"/rooms/{room_id}/messages/{mid}"),
        await alice.get(f"/rooms/{room_id}/messages/{uuid.uuid4()}"),  # 404
        await edit(alice, room_id, mid, "y", 1),
        await edit(alice, room_id, mid, "z", 1),  # 409
        await delete(alice, room_id, mid, 2),  # 204
        await alice.client.post(  # 413
            f"/rooms/{room_id}/messages",
            content=b"x" * 200_000,
            headers={"Content-Type": "application/json"},
        ),
    ]
    assert [r.status_code for r in responses] == [
        201,
        200,
        409,
        403,
        422,
        200,
        200,
        404,
        200,
        409,
        204,
        413,
    ]
    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.status_code


# ---- secrets and content stay out of logs and errors --------------------------------------------------------


async def test_an_unexpected_exception_never_leaks_the_body_to_the_client_or_logs(
    alice, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    marker = "SECRET-BODY-IN-AN-EXCEPTION-4411"

    async def explode(*_args, **_kwargs):
        raise RuntimeError(f"database said: {marker}")

    monkeypatch.setattr(messages_service, "_consume_write_capacity", explode)
    room_id = await new_room(alice)
    response = await send(alice, room_id, marker)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert marker not in response.text
    logged = "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records)
    assert marker not in logged  # neither the exception text nor the request body was logged


async def test_a_full_message_lifecycle_leaves_no_secrets_in_the_logs(alice, sessionmaker, caplog):
    caplog.set_level(logging.DEBUG)
    room_id = await new_room(alice)
    body = "PRIVATE-MESSAGE-CONTENT-5521"
    cookie = alice.client.cookies.get("tf_session")
    csrf = await alice.csrf_token()
    sent = (await send(alice, room_id, body)).json()
    await edit(alice, room_id, sent["id"], body + " edited", 1)
    await alice.get(f"/rooms/{room_id}/messages")
    await delete(alice, room_id, sent["id"], 2)
    [stored] = await rows(sessionmaker, "SELECT initial_request_fingerprint FROM messages")
    fingerprint = bytes(stored["initial_request_fingerprint"])

    logged = "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records)
    assert "action=message_sent" in logged and "action=message_deleted" in logged  # lines exist
    for secret in (
        body,
        cookie,
        csrf,
        fingerprint.hex(),
        base64.b64encode(fingerprint).decode(),
        base64.urlsafe_b64encode(fingerprint).decode(),
    ):
        assert secret and secret not in logged


async def test_validation_and_cursor_errors_never_echo_input(alice):
    room_id = await new_room(alice)
    marker = "ECHO-CHECK-8842"
    responses = [
        await alice.get(f"/rooms/{room_id}/messages", params={"limit": marker}),
        await alice.get(f"/rooms/{room_id}/messages", params={"before": marker}),
        await alice.delete(
            f"/rooms/{room_id}/messages/{uuid.uuid4()}", params={"expected_version": marker}
        ),
        await alice.patch(
            f"/rooms/{room_id}/messages/{uuid.uuid4()}",
            json={"body": marker, "expected_version": marker},
        ),
    ]
    assert [r.status_code for r in responses] == [422] * 4
    assert not any(marker in r.text for r in responses)


# ---- prerequisites the design relies on ---------------------------------------------------------------------


async def test_engine_hides_parameters_has_no_echo_and_runs_read_committed(engine):
    sync_engine = engine.sync_engine
    assert (
        sync_engine.hide_parameters is True
    )  # bound parameters (message bodies) stay out of errors
    assert not sync_engine.echo  # SQL is not logged (the attribute is None/False when unset)
    async with engine.connect() as connection:
        from sqlalchemy import text

        level = (await connection.execute(text("SHOW transaction_isolation"))).scalar_one()
    assert level == "read committed"  # never raised: in-lock lookups rely on seeing fresh commits


def test_the_engine_is_never_configured_with_a_non_default_isolation_level():
    assert "isolation_level" not in Path("app/db.py").read_text(encoding="utf-8")


def test_only_the_message_service_and_cleanup_touch_the_message_tables():
    """Routes and other services reach messages only through app.services.messages; the tables
    themselves are referenced only by the models, the message service, and the cleanup command
    (which deletes old rate-limit buckets and never messages)."""
    allowed = {
        Path("app/services/messages.py"),
        Path("app/commands/cleanup_auth.py"),
        Path("app/models/message.py"),
        Path("app/models/message_write_bucket.py"),
        Path("app/models/__init__.py"),
    }
    table_access = re.compile(
        r"_MESSAGES|_BUCKETS|Message\.__table__|MessageWriteBucket|message_write_buckets"
        r"|(FROM|INTO|UPDATE)\s+messages"
    )
    for path in Path("app").rglob("*.py"):
        if path not in allowed:
            assert not table_access.search(path.read_text(encoding="utf-8")), (
                f"{path} touches the tables"
            )
    cleanup = Path("app/commands/cleanup_auth.py").read_text(encoding="utf-8")
    assert "_MESSAGES" not in cleanup and "Message.__table__" not in cleanup
