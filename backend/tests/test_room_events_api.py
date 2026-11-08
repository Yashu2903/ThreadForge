"""GET /rooms/{room_id}/events: durable history, authoritative from PostgreSQL."""

import asyncio
import uuid

import pytest

from tests.helpers import add_member, execute, scalar
from tests.test_invitations_api import new_room
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio

EVENT_KEYS = [
    "schema_version",
    "event_id",
    "room_id",
    "room_seq",
    "event_type",
    "resource_type",
    "resource_id",
    "resource_version",
    "actor_id",
    "payload",
    "created_at",
]


async def fill(alice, room_id, count):
    for i in range(count):
        response = await send(alice, room_id, f"m{i}")
        assert response.status_code == 201, response.text


async def test_lists_events_in_order_with_the_documented_shape(alice):
    room_id = await new_room(alice)
    await fill(alice, room_id, 3)
    response = await alice.get(f"/rooms/{room_id}/events")
    assert response.status_code == 200
    body = response.json()
    assert [e["room_seq"] for e in body["items"]] == [1, 2, 3, 4]
    assert [e["event_type"] for e in body["items"]] == ["room_created"] + ["message_created"] * 3
    assert list(body["items"][0]) == EVENT_KEYS
    assert body["current_room_seq"] == 4 and body["next_after_seq"] == 4
    assert body["has_more"] is False
    assert response.headers["cache-control"] == "no-store"


async def test_pagination_walks_every_event_exactly_once(alice):
    room_id = await new_room(alice)
    await fill(alice, room_id, 6)  # seq 1..7
    seen, cursor = [], 0
    while True:
        body = (await alice.get(f"/rooms/{room_id}/events?after_seq={cursor}&limit=3")).json()
        seen += [e["room_seq"] for e in body["items"]]
        cursor = body["next_after_seq"]
        if not body["has_more"]:
            break
    assert seen == [1, 2, 3, 4, 5, 6, 7]


async def test_empty_page_keeps_the_cursor_and_reports_current_seq(alice):
    room_id = await new_room(alice)
    body = (await alice.get(f"/rooms/{room_id}/events?after_seq=1")).json()
    assert body == {"items": [], "next_after_seq": 1, "has_more": False, "current_room_seq": 1}


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=501",
        "limit=-1",
        "limit=abc",
        "after_seq=-1",
        "after_seq=abc",
        "after_seq=9223372036854775808",
    ],
)
async def test_out_of_range_parameters_are_422_never_clamped(alice, query):
    room_id = await new_room(alice)
    response = await alice.get(f"/rooms/{room_id}/events?{query}")
    assert response.status_code == 422


async def test_limit_bounds_are_accepted(alice):
    room_id = await new_room(alice)
    assert (await alice.get(f"/rooms/{room_id}/events?limit=1")).status_code == 200
    assert (await alice.get(f"/rooms/{room_id}/events?limit=500")).status_code == 200


async def test_cursor_ahead_is_409_with_current_room_seq(alice):
    room_id = await new_room(alice)
    response = await alice.get(f"/rooms/{room_id}/events?after_seq=2")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "cursor_ahead"
    assert error["current_room_seq"] == 1
    # equal to the latest is fine (nothing newer)
    assert (await alice.get(f"/rooms/{room_id}/events?after_seq=1")).status_code == 200


async def test_nonmembers_removed_members_and_unknown_rooms_look_identical(
    alice, bob, sessionmaker
):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    assert (await bob.get(f"/rooms/{room_id}/events")).status_code == 200  # viewers may read
    await add_member(sessionmaker, room_id, bob.id, "viewer", state="removed")
    removed = await bob.get(f"/rooms/{room_id}/events")
    unknown = await bob.get(f"/rooms/{uuid.uuid4()}/events")
    assert removed.status_code == unknown.status_code == 404
    assert removed.json()["error"]["code"] == unknown.json()["error"]["code"] == "room_not_found"


async def test_requires_a_session(alice, client):
    room_id = await new_room(alice)
    assert (await client.get(f"/rooms/{room_id}/events")).status_code == 401


async def test_history_does_not_leak_text_tokens_or_credentials(alice, sessionmaker):
    room_id = await new_room(alice)
    await send(alice, room_id, "TOP-SECRET-BODY")
    text = (await alice.get(f"/rooms/{room_id}/events")).text
    for forbidden in ("token", "cookie", "fingerprint", "secret_hash", "body"):
        assert forbidden not in text.lower(), forbidden
    assert "TOP-SECRET-BODY" not in text


async def test_history_is_independent_of_redis(alice, settings, engine, make_actor):
    """The endpoint never touches Redis: an unreachable Redis URL changes nothing."""
    import httpx2

    from app.main import create_app
    from tests.factories import make_settings

    broken = make_settings(redis_url="redis://127.0.0.1:1/15", redis_socket_timeout_seconds=0.2)
    user = await make_actor("Dana", app_settings=broken)
    app = create_app(broken, engine=engine)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        c.cookies.set(broken.session_cookie_name, user.secret)
        created = await c.post(
            "/rooms",
            json={"name": "NoRedis"},
            headers={
                "Origin": "http://localhost:8000",
                "X-CSRF-Token": (await c.get("/auth/csrf")).json()["csrf_token"],
            },
        )
        assert created.status_code == 201
        room_id = created.json()["id"]
        events = await c.get(f"/rooms/{room_id}/events")
        assert events.status_code == 200 and events.json()["current_room_seq"] == 1
        assert (await c.get("/health/ready")).status_code == 200


async def test_items_and_current_room_seq_agree_under_concurrent_writes(alice, bob, sessionmaker):
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    stop = asyncio.Event()

    async def writer(actor, tag):
        i = 0
        while not stop.is_set():
            await send(actor, room_id, f"{tag}{i}")
            i += 1

    writers = [asyncio.create_task(writer(alice, "a")), asyncio.create_task(writer(bob, "b"))]
    try:
        for _ in range(25):
            body = (await alice.get(f"/rooms/{room_id}/events?limit=500")).json()
            seqs = [e["room_seq"] for e in body["items"]]
            assert seqs == list(range(1, len(seqs) + 1))  # contiguous: no gaps ever visible
            assert seqs[-1] <= body["current_room_seq"]
            # one snapshot: with everything returned, the page ends exactly at current_room_seq
            if not body["has_more"]:
                assert seqs[-1] == body["current_room_seq"]
    finally:
        stop.set()
        await asyncio.gather(*writers)
    final = await scalar(sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id)
    assert final == await scalar(
        sessionmaker, "SELECT count(*) FROM room_events WHERE room_id = :r", r=room_id
    )


async def test_events_endpoint_is_read_only(alice, sessionmaker):
    room_id = await new_room(alice)
    await fill(alice, room_id, 2)
    before = await scalar(sessionmaker, "SELECT count(*) FROM outbox")
    await alice.get(f"/rooms/{room_id}/events")
    await execute(sessionmaker, "SELECT 1")
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox") == before
