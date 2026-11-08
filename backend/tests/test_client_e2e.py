"""The REAL TypeScript client (client/dist, run by Node) against a REAL backend: real uvicorn, real
PostgreSQL, real Redis. This is where the two halves must agree on the protocol.

Needs Node 22 and a built client: `cd client && npm install && npm run build`. If those are
missing the tests fail with that instruction (they are never silently skipped).
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from tests.helpers import add_member, execute
from tests.realtime_helpers import live_server, wait_until
from tests.test_invitations_api import new_room
from tests.test_members_api import member_path
from tests.test_messages_edit_delete import delete, edit
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio

CLIENT_DIR = Path(__file__).resolve().parents[2] / "client"
DRIVER = CLIENT_DIR / "test" / "e2e-driver.mjs"


@pytest.fixture(scope="module", autouse=True)
def _client_is_built():
    if shutil.which("node") is None or not (CLIENT_DIR / "dist" / "index.js").exists():
        pytest.fail(
            "Node and a built client are required: cd client && npm install && npm run build"
        )


class Driver:
    """A Node process running the real RoomClient; its state lines are collected in a list."""

    def __init__(self, server, room_id, actor, reconnect_ms=200):
        env = {
            **os.environ,
            "TF_BASE_URL": server.http_url,
            "TF_ORIGIN": "http://localhost:8000",
            "TF_COOKIE": actor.secret,
            "TF_ROOM": room_id,
            "TF_USER": str(actor.id),
            "TF_RECONNECT_MS": str(reconnect_ms),
        }
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-only
            ["node", str(DRIVER)],  # noqa: S607
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=CLIENT_DIR,
        )
        self.lines: list[dict] = []
        self.raw: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.raw.append(line)
            with contextlib.suppress(ValueError):
                self.lines.append(json.loads(line))

    @property
    def state(self) -> dict:
        return next((line for line in reversed(self.lines) if "status" in line), {})

    def command(self, text: str) -> None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    async def until(self, predicate, within=10.0, message=""):
        await wait_until(
            lambda: predicate(self.state), within, 0.05, message + f" state={self.state}"
        )

    def bodies(self) -> dict[str, tuple]:
        return {m["id"]: (m["body"], m["v"], m["deleted"]) for m in self.state.get("messages", [])}

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


@pytest_asyncio.fixture
async def stack(settings, engine, redis_client):
    fast = settings.model_copy(
        update={
            "presence_tick_seconds": 0.5,
            "presence_hint_min_interval_seconds": 0.05,
            "typing_state_min_interval_seconds": 0.05,
            "reconcile_interval_seconds": 0.5,
            "reconcile_interval_degraded_seconds": 0.3,
            "access_check_interval_seconds": 1.0,
            "access_cache_seconds": 0.1,
            "typing_expiry_seconds": 1.5,
            "presence_expiry_seconds": 3.0,
            "client_heartbeat_interval_seconds": 1.0,
            "message_writes_per_minute": 1000,
        }
    )
    drivers: list[Driver] = []
    async with live_server(fast, engine) as server:
        yield server, drivers
    for d in drivers:
        d.kill()


async def start(drivers, server, room_id, actor, **kw):
    d = Driver(server, room_id, actor, **kw)
    drivers.append(d)
    await d.until(lambda s: s.get("status") == "live", message="never went live")
    return d


async def test_two_clients_bootstrap_chat_see_presence_and_converge(
    stack, alice, bob, sessionmaker
):
    server, drivers = stack
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "viewer")
    await send(alice, room_id, "before anyone connected")
    a = await start(drivers, server, room_id, alice)
    b = await start(drivers, server, room_id, bob)
    assert [v[0] for v in b.bodies().values()] == ["before anyone connected"]  # from the bootstrap
    await a.until(lambda s: len(s["online"]) == 2, message="presence")
    b.command("send hello from bob")  # a viewer can chat
    await a.until(lambda s: "hello from bob" in {m["body"] for m in s["messages"]}, message="live")
    assert a.bodies().keys() == b.bodies().keys()
    assert a.state["cursor"] == b.state["cursor"]


async def test_a_client_offline_for_edits_and_deletes_converges_without_duplicates_or_resurrected_text(
    stack, alice, bob, sessionmaker
):
    server, drivers = stack
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    created = (await send(alice, room_id, "first")).json()
    doomed = (await send(alice, room_id, "to be deleted")).json()
    a = await start(drivers, server, room_id, alice)
    b = await start(drivers, server, room_id, bob, reconnect_ms=3000)  # offline ~3 s after a drop
    b.command("drop")  # abrupt network failure
    await b.until(lambda s: s["status"] == "reconnecting", message="did not notice the drop")
    await edit(alice, room_id, created["id"], "first (edited)", 1)
    await delete(alice, room_id, doomed["id"], 1)
    third = (await send(alice, room_id, "written while bob was offline")).json()
    await b.until(lambda s: s["status"] == "live", within=15, message="never reconnected")
    await b.until(lambda s: len(s["messages"]) == 3, message="missing messages")
    assert b.bodies() == {
        created["id"]: ("first (edited)", 2, False),
        doomed["id"]: (None, 2, True),  # the tombstone: no resurrected text
        third["id"]: ("written while bob was offline", 1, False),
    }
    assert len(b.state["messages"]) == len({m["id"] for m in b.state["messages"]})  # no duplicates
    await a.until(lambda s: s["status"] == "live")
    assert a.bodies() == b.bodies() and a.state["cursor"] == b.state["cursor"]


async def test_removing_a_member_ends_their_client_and_clears_the_room(
    stack, alice, bob, sessionmaker
):
    server, drivers = stack
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    await send(alice, room_id, "secret room content")
    a = await start(drivers, server, room_id, alice)
    b = await start(drivers, server, room_id, bob)
    assert (await alice.delete(member_path(room_id, bob))).status_code in (200, 204)
    await b.until(lambda s: s["status"] == "terminal", message="not terminal")
    assert b.state["terminal"] == "access_lost"
    assert b.state["messages"] == [] and b.state["room"] is None  # room data cleared
    assert not any("member_removed" in line for line in b.raw)
    await a.until(lambda s: len(s["members"]) == 1)
    seen = len(b.lines)
    await asyncio.sleep(1.5)
    assert len(b.lines) == seen  # no reconnect attempts afterwards


async def test_a_revoked_session_stops_the_client_and_it_does_not_reconnect(
    stack, alice, bob, sessionmaker
):
    server, drivers = stack
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    b = await start(drivers, server, room_id, bob)
    await execute(
        sessionmaker, "UPDATE sessions SET revoked_at = now() WHERE user_id = :u", u=bob.id
    )
    # the periodic access check (1 s here) closes the idle socket with 4401: sign in again
    await b.until(lambda s: s["status"] == "terminal", within=8, message="idle revoked session")
    assert b.state["terminal"] == "sign_in_required"


async def test_a_refused_handshake_after_removal_is_noticed_through_the_bootstrap(
    stack, alice, bob, sessionmaker
):
    """Offline when removed: the reconnect handshake is refused with 403, which a browser (and
    this Node client, mimicking one) only sees as close code 1006. The client must still find out."""
    server, drivers = stack
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    b = await start(drivers, server, room_id, bob, reconnect_ms=2500)
    b.command("drop")
    await b.until(lambda s: s["status"] == "reconnecting")
    await add_member(sessionmaker, room_id, bob.id, "editor", state="removed")  # removed meanwhile
    await b.until(lambda s: s["status"] == "terminal", within=15, message="never noticed")
    assert b.state["terminal"] == "access_lost" and b.state["messages"] == []


async def test_typing_and_availability_flow_between_real_clients(stack, alice, bob, sessionmaker):
    server, drivers = stack
    room_id = await new_room(alice)
    await add_member(sessionmaker, room_id, bob.id, "editor")
    a = await start(drivers, server, room_id, alice)
    b = await start(drivers, server, room_id, bob)
    await a.until(lambda s: len(s["online"]) == 2)
    b.command("typing")
    await a.until(lambda s: s["typing"] == [str(bob.id)], message="typing not shown")
    await a.until(lambda s: s["typing"] == [], within=8, message="typing did not expire")
    b.command("avail dnd")
    await a.until(lambda s: f"{bob.id}:dnd" in s["online"], message="availability not shown")


async def test_a_retried_send_never_duplicates(stack, alice, sessionmaker):
    server, drivers = stack
    room_id = await new_room(alice)
    a = await start(drivers, server, room_id, alice)
    cid = str(uuid.uuid4())
    for _ in range(3):  # the same client_message_id, as a retrying client would
        response = await send(alice, room_id, "once", cmid=cid)
        assert response.status_code in (200, 201)
    await a.until(lambda s: len(s["messages"]) == 1, message="duplicate or missing")
    await asyncio.sleep(0.5)
    assert len(a.state["messages"]) == 1


async def test_a_client_too_far_behind_resyncs_through_a_fresh_bootstrap_and_converges(
    settings, engine, redis_client, alice, bob, sessionmaker
):
    """The server refuses to replay more than REPLAY_MAX_EVENTS: resync_required(too_far_behind),
    close 4410. The real client must then fetch a bootstrap, replace state and cursor together,
    and resume, ending exactly where a fresh bootstrap says."""
    from tests.helpers import append_events

    cfg = settings.model_copy(
        update={"replay_max_events": 10, "replay_page_size": 5, "message_writes_per_minute": 1000}
    )
    drivers: list[Driver] = []
    try:
        async with live_server(cfg, engine) as server:
            room_id = await new_room(alice)
            await add_member(sessionmaker, room_id, bob.id, "editor")
            await send(alice, room_id, "before the gap")
            b = await start(drivers, server, room_id, bob, reconnect_ms=2500)
            b.command("drop")
            await b.until(lambda s: s["status"] == "reconnecting")
            await append_events(sessionmaker, uuid.UUID(room_id), 30)  # 30 events while B is away
            last = (await send(alice, room_id, "after the gap")).json()
            await b.until(
                lambda s: s["status"] == "live" and last["id"] in {m["id"] for m in s["messages"]},
                within=20,
                message="did not converge",
            )
            head = await __import__("tests.helpers", fromlist=["scalar"]).scalar(
                sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id
            )
            assert b.state["cursor"] == head
            assert server.hub.counters.resyncs["too_far_behind"] == 1
            assert {m["body"] for m in b.state["messages"]} == {"before the gap", "after the gap"}
    finally:
        for d in drivers:
            d.kill()
