"""Convergence under load: many real clients, random disconnects, concurrent writers.

Each client keeps ONLY a cursor, like the real one: it applies an event only if it is cursor + 1
(a gap or a repeat would be a protocol violation and is recorded), reconnects from its cursor after
a random drop, and acknowledges as it goes. At the end every client must have applied exactly
1..event_seq, once each, in order.
"""

import asyncio
import json
import random

import pytest

from tests.helpers import add_member, scalar
from tests.realtime_helpers import live_server, send_json, send_resume, wait_until, ws_connect
from tests.test_invitations_api import new_room
from tests.test_messages_edit_delete import delete, edit
from tests.test_messages_send import send

pytestmark = pytest.mark.asyncio


class StressClient:
    def __init__(self, server, room_id, actor, seed):
        self.server, self.room_id, self.actor = server, room_id, actor
        self.rng = random.Random(seed)
        self.cursor = 0
        self.applied: list[int] = []
        self.violations: list[str] = []
        self.reconnects = 0
        self.target: int | None = None
        self.finished = asyncio.Event()

    async def run(self):
        while not self.finished.is_set():
            try:
                await self._session()
            except Exception as exc:  # noqa: BLE001 - a dropped or refused socket: just reconnect
                self.violations_if_fatal(exc)
            self.reconnects += 1
            await asyncio.sleep(self.rng.random() * 0.05)

    def violations_if_fatal(self, exc):
        text = str(exc)
        if "4429" in text or "4400" in text or "4410" in text or "4403" in text or "4401" in text:
            self.violations.append(f"closed by server: {text}")

    async def _session(self):
        ws = await ws_connect(self.server, self.room_id, self.actor.secret)
        try:
            await send_resume(ws, self.cursor)
            drop_at = asyncio.get_running_loop().time() + (
                self.rng.uniform(0.05, 0.5) if self.rng.random() < 0.6 else 1e9
            )
            since_ack = 0
            while not self.finished.is_set():
                remaining = drop_at - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await ws.close()  # a random clean drop
                    return
                try:
                    frame = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=min(remaining, 0.2))
                    )
                except TimeoutError:
                    continue
                if frame["type"] != "room_event":
                    if frame["type"] == "resync_required":
                        self.violations.append("resync_required")
                    continue
                seq = frame["room_seq"]
                if seq <= self.cursor:
                    self.violations.append(f"repeat {seq} (cursor {self.cursor})")
                    continue
                if seq != self.cursor + 1:
                    self.violations.append(f"gap: {seq} after {self.cursor}")
                    return
                self.cursor = seq
                self.applied.append(seq)
                since_ack += 1
                if since_ack >= 40:
                    await send_json(ws, type="ack", through_seq=self.cursor)
                    since_ack = 0
                if self.target is not None and self.cursor >= self.target:
                    self.finished.set()
        finally:
            with __import__("contextlib").suppress(Exception):
                await ws.close()


async def test_many_clients_with_random_disconnects_all_converge_on_the_same_sequence(
    settings, engine, redis_client, make_actor, sessionmaker, pump
):
    cfg = settings.model_copy(
        update={
            "message_writes_per_minute": 1000,
            "ws_max_connections_per_user": 10,
            "reconcile_interval_seconds": 0.3,
            "access_cache_seconds": 0.1,
            "access_check_interval_seconds": 5.0,
            "ack_window": 60,
            "replay_page_size": 25,
        }
    )
    users = [await make_actor(f"U{i}", app_settings=cfg) for i in range(6)]
    async with live_server(cfg, engine) as server:
        room_id = await new_room(users[0])
        for user in users[1:]:
            await add_member(sessionmaker, room_id, user.id, "editor")
        clients = [StressClient(server, room_id, users[i % 6], seed=i) for i in range(24)]
        tasks = [asyncio.create_task(c.run()) for c in clients]

        async def writer(user, tag):
            for i in range(14):
                created = (await send(user, room_id, f"{tag}{i}")).json()
                if i % 3 == 0:
                    await edit(user, room_id, created["id"], f"{tag}{i}!", 1)
                if i % 5 == 0:
                    await delete(user, room_id, created["id"], 2 if i % 3 == 0 else 1)
                await pump.flush()

        await asyncio.gather(*[writer(users[i], "abcd"[i]) for i in range(4)])
        await pump.flush()
        target = await scalar(sessionmaker, "SELECT event_seq FROM rooms WHERE id = :r", r=room_id)
        for client in clients:
            client.target = target
            if client.cursor >= target:
                client.finished.set()
        await asyncio.wait_for(asyncio.gather(*[c.finished.wait() for c in clients]), timeout=60)
        await asyncio.gather(*tasks, return_exceptions=True)

        for index, client in enumerate(clients):
            assert client.violations == [], (index, client.violations)
            assert client.applied == list(range(1, target + 1)), (index, len(client.applied))
        assert sum(c.reconnects for c in clients) > len(clients)  # the random drops really happened
        assert server.hub.counters.resyncs == {}  # nobody ever needed a resync
        await wait_until(lambda: server.hub.active_connections == 0, 10)
        assert server.hub.rooms == {} and server.hub.replaying == 0
        await server.hub.manager.wait_idle()
        assert server.hub.manager.reference_counts == {}
