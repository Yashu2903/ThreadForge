"""Manual Part 5 demonstration: bootstrap, replay after a disconnect, presence, typing, live access.

THREE REAL signed-in accounts: A = owner, B = member (logs out at the very end), C = member
(removed). Sign in through the real Google flow in three browser profiles and copy each
`tf_session` cookie VALUE into an environment variable without putting it on a command line or into
a transcript (PowerShell):

    $env:TF_COOKIE_A = Read-Host "cookie A" -MaskInput
    $env:TF_COOKIE_B = Read-Host "cookie B" -MaskInput
    $env:TF_COOKIE_C = Read-Host "cookie C" -MaskInput

Start everything (README "Part 5"): Postgres + Redis (docker compose up -d --wait), two API
processes (ports 8000 and 8001) and the dispatcher, then from backend/:

    uv run python -m scripts.manual_part5_check

Environment: TF_BASE_URL_1 / TF_BASE_URL_2 (default http://localhost:8000 / :8001), TF_COOKIE_NAME
(default tf_session; production __Host-tf_session), TF_ORIGIN (must be in TRUSTED_ORIGINS; default
base url 1), TF_OPERATOR=1 to be prompted for the steps that need you to stop/start the dispatcher,
Redis or an API process (otherwise they are SKIPPED).

The script contains a tiny client: it installs a bootstrap and its cursor TOGETHER, resumes from
that cursor, fetches what each batch of events refers to, merges by (id, version), and advances the
cursor only afterwards. Convergence is judged against a FRESH bootstrap, never against itself.

Prints PASS/FAIL/SKIP per step; never prints cookies, tokens, CSRF values or message text. A frame
proves the server queued it, not that a browser rendered it. Exit code 1 if any check failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

# (action, instruction) -> True if performed. Actions: stop_dispatcher, start_dispatcher,
# stop_redis, start_redis, restart_api1.
Operator = Callable[[str, str], Awaitable[bool]]
WAIT = 20.0
CHATTER = {"presence_snapshot", "typing_state", "heartbeat_ack"}


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if not ok:
            self.failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")
        return ok

    def skip(self, label: str, why: str) -> None:
        print(f"[SKIP] {label} - {why}")


async def until(predicate: Callable[[], bool], within: float = WAIT) -> bool:
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


class Replica:
    """A tiny client state: messages by id + a cursor, installed and advanced together."""

    def __init__(self) -> None:
        self.cursor = 0
        self.messages: dict[
            str, tuple[int, str | None, bool]
        ] = {}  # id -> (version, body, deleted)
        self.seen: list[int] = []  # every event seq applied, in order

    def install(self, boot: dict[str, Any]) -> None:
        self.messages = {
            m["id"]: (m["version"], m["body"], m["is_deleted"]) for m in boot["messages"]
        }
        self.cursor = boot["through_seq"]

    def merge(self, m: dict[str, Any]) -> None:
        local = self.messages.get(m["id"])
        if local and (local[2] or m["version"] < local[0]):  # tombstones are final; no downgrades
            return
        self.messages[m["id"]] = (m["version"], m["body"], m["is_deleted"])


class Sock:
    """One WebSocket with a background reader that records everything it receives."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ws: ClientConnection | None = None
        self.events: list[dict[str, Any]] = []
        self.presence: dict[str, Any] | None = None
        self.typing: dict[str, Any] | None = None
        self.sync: list[dict[str, Any]] = []
        self.closed: int | None = None
        self.done = False
        self._task: asyncio.Task[None] | None = None

    async def open(
        self, base: str, room: str, cookie_name: str, cookie: str, origin: str, after_seq: int
    ) -> None:
        url = base.replace("http", "ws", 1) + f"/rooms/{room}/ws"
        self.ws = await connect(
            url,
            additional_headers={"Cookie": f"{cookie_name}={cookie}", "Origin": origin},
            origin=None,
            open_timeout=10,
            ping_interval=None,
            max_queue=None,
        )
        await self.send(type="resume", schema_version=1, after_seq=after_seq)
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.ws is not None
        try:
            with contextlib.suppress(Exception):  # the socket closing ends the loop
                async for raw in self.ws:
                    frame = json.loads(raw)
                    kind = frame["type"]
                    if kind == "room_event":
                        self.events.append(frame)
                    elif kind == "presence_snapshot":
                        self.presence = frame
                    elif kind == "typing_state":
                        self.typing = frame
                    elif kind in ("sync_started", "sync_complete", "resync_required"):
                        self.sync.append(frame)
        finally:
            if self.ws is not None and self.ws.close_code is not None:
                self.closed = self.ws.close_code
            self.done = True

    async def send(self, **frame: Any) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps(frame))

    @property
    def synced(self) -> bool:
        return any(f["type"] == "sync_complete" for f in self.sync)

    def online(self) -> dict[str, str]:
        return {
            e["user_id"]: e["availability"] for e in (self.presence or {"online": []})["online"]
        }

    async def close(self) -> None:
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self._task:
            self._task.cancel()


async def run(  # noqa: PLR0915 - a linear script reads best top to bottom
    clients: dict[str, httpx2.AsyncClient],
    *,
    base1: str,
    base2: str,
    cookie_name: str,
    cookies: dict[str, str],
    origin: str,
    operator: Operator | None = None,
    typing_wait: float = 12.0,
) -> int:
    report = Report()
    a, b, c = clients["a"], clients["b"], clients["c"]
    csrf = {}
    for key, client in clients.items():
        r = await client.get("/auth/csrf")
        if r.status_code != 200:
            raise SystemExit(f"/auth/csrf returned {r.status_code}: is the cookie valid?")
        csrf[key] = {"X-CSRF-Token": r.json()["csrf_token"], "Origin": origin}
    sockets: list[Sock] = []

    async def open_socket(name: str, who: str, base: str, after_seq: int) -> Sock:
        sock = Sock(name)
        sockets.append(sock)
        await sock.open(base, room, cookie_name, cookies[who], origin, after_seq)
        return sock

    async def send_message(client: httpx2.AsyncClient, key: str, text: str) -> dict[str, Any]:
        r = await client.post(
            f"/rooms/{room}/messages",
            json={"client_message_id": str(uuid.uuid4()), "body": text},
            headers=csrf[key],
        )
        return r.json() if r.status_code in (200, 201) else {}

    async def apply(replica: Replica, sock: Sock, client: httpx2.AsyncClient) -> None:
        """Process the socket's events like the real client: fetch what they refer to, merge by
        (id, version), and advance the cursor only after every fetch succeeded."""
        batch = [e for e in sock.events if e["room_seq"] > replica.cursor]
        for e in batch:  # ascending and gapless, or the stream is broken
            expected = (replica.seen[-1] if replica.seen else replica.cursor) + 1
            if e["room_seq"] != expected:
                raise AssertionError(f"gap or reorder: expected {expected}, got {e['room_seq']}")
            replica.seen.append(e["room_seq"])
        for message_id in {e["resource_id"] for e in batch if e["resource_type"] == "message"}:
            r = await client.get(f"/rooms/{room}/messages/{message_id}")
            if r.status_code == 200:
                replica.merge(r.json())
        if batch:
            replica.cursor = batch[-1]["room_seq"]
            await sock.send(type="ack", through_seq=replica.cursor)

    async def fresh(client: httpx2.AsyncClient) -> dict[str, Any]:
        return (await client.get(f"/rooms/{room}/sync")).json()

    try:
        # 1. A creates a room; B and C join as members.
        r = await a.post("/rooms", json={"name": "Part 5 demo"}, headers=csrf["a"])
        if r.status_code != 201:
            report.check(False, "1. A creates a room", f"HTTP {r.status_code}")
            return 1
        room = r.json()["id"]
        joined = []
        for role, key in (("editor", "b"), ("viewer", "c")):
            inv = await a.post(f"/rooms/{room}/invitations", json={"role": role}, headers=csrf["a"])
            red = await clients[key].post(
                "/invitations/redeem", json={"token": inv.json()["token"]}, headers=csrf[key]
            )
            joined.append(red.status_code)
        report.check(joined == [200, 200], "1. A created the room; B and C joined", str(joined))

        # 2. Bootstrap, then resume from through_seq: synchronized, presence both ways.
        boot_a, boot_b = await fresh(a), await fresh(b)
        replica_a, replica_b = Replica(), Replica()
        replica_a.install(boot_a)
        replica_b.install(boot_b)
        ok = (
            boot_a["through_seq"] == boot_b["through_seq"] >= 3
            and len(boot_a["members"]) == 3
            and "email" not in json.dumps(boot_a)
        )
        report.check(ok, "2a. bootstrap: state and through_seq from one snapshot, no emails")
        sa = await open_socket("A1", "a", base1, replica_a.cursor)
        sb = await open_socket("B1", "b", base2, replica_b.cursor)  # a DIFFERENT API process
        synced = await until(lambda: sa.synced and sb.synced)
        report.check(
            synced
            and sa.sync[0]["after_seq"] == replica_a.cursor
            and sa.sync[-1]["through_seq"] == boot_a["through_seq"],
            "2b. both sockets resumed from through_seq and reached sync_complete",
        )
        both = await until(lambda: len(sa.online()) >= 2 and len(sb.online()) >= 2)
        report.check(
            both and sa.presence["degraded"] is False, "2c. presence: A and B see each other"
        )

        # 3. Live message A -> B.
        first = await send_message(a, "a", "first message")
        got = await until(lambda: any(e["resource_id"] == first.get("id") for e in sb.events))
        await apply(replica_b, sb, b)
        report.check(
            got and replica_b.messages.get(first.get("id"), (0,))[0] == 1,
            "3. a message written over HTTP reaches B's socket and B's replica",
        )

        # 4. Two tabs for one account are ONE online user.
        sa2 = await open_socket("A2", "a", base1, replica_a.cursor)
        await until(lambda: sa2.synced)
        await asyncio.sleep(1.5)
        report.check(
            sb.presence is not None and len(sb.online()) == 2,
            "4. a second tab for A does not add a second online user",
            f"online={len(sb.online())}",
        )
        await sa2.close()
        await asyncio.sleep(1.0)
        report.check(len(sb.online()) == 2, "4b. closing one of A's tabs does not show A offline")

        # 5. Typing appears and expires by itself.
        await sb.send(type="typing", state="start")
        shown = await until(lambda: sa.typing is not None and len(sa.typing["typing"]) == 1)
        gone = await until(lambda: sa.typing is not None and sa.typing["typing"] == [], typing_wait)
        report.check(shown and gone, "5. typing shows for others and expires without a stop frame")

        # 6. B disconnects; changes happen; B resumes from its cursor and converges.
        await apply(replica_b, sb, b)
        await sb.close()
        sb.done = True
        second = await send_message(a, "a", "written while B is away")
        doomed = await send_message(a, "a", "this will be deleted")
        await a.patch(
            f"/rooms/{room}/messages/{first['id']}",
            json={"body": "first message (edited)", "expected_version": 1},
            headers=csrf["a"],
        )
        await a.delete(
            f"/rooms/{room}/messages/{doomed['id']}",
            params={"expected_version": 1},
            headers=csrf["a"],
        )
        sb = await open_socket("B2", "b", base2, replica_b.cursor)
        await until(lambda: sb.synced)
        await asyncio.sleep(0.5)
        await apply(replica_b, sb, b)
        truth = await fresh(b)
        expected = {m["id"]: (m["version"], m["body"], m["is_deleted"]) for m in truth["messages"]}
        seqs = [e["room_seq"] for e in sb.events]
        report.check(
            replica_b.messages == expected and replica_b.cursor == truth["through_seq"],
            "6a. after replay B's state equals a fresh bootstrap (converged)",
        )
        report.check(
            seqs == sorted(set(seqs)) and len(seqs) == 4,
            "6b. replay was ordered and had no duplicates",
            str(seqs),
        )
        tomb = replica_b.messages.get(doomed["id"])
        report.check(
            bool(tomb) and tomb[1] is None and tomb[2] and second["id"] in replica_b.messages,
            "6c. the deleted message is a tombstone: its text did not come back",
        )

        # 7. The dispatcher's publish is suppressed: reconciliation still delivers it.
        if operator is None:
            report.skip("7. dispatcher stopped", "run with TF_OPERATOR=1 to perform this step")
        elif await operator("stop_dispatcher", "Stop the outbox dispatcher now."):
            third = await send_message(a, "a", "no hint will ever be published for this")
            late = await until(lambda: any(e["resource_id"] == third.get("id") for e in sb.events))
            await apply(replica_b, sb, b)
            report.check(
                late and third.get("id") in replica_b.messages,
                "7. with no dispatcher the event still arrives (reconciliation, one interval)",
            )
            await operator("start_dispatcher", "Start the outbox dispatcher again.")
        else:
            report.skip("7. dispatcher stopped", "operator skipped")

        # 8. Redis stopped: durable delivery continues; presence says "unknown"; then it rebuilds.
        if operator is None:
            report.skip("8. Redis stopped", "run with TF_OPERATOR=1 to perform this step")
        elif await operator("stop_redis", "Stop Redis now (docker compose stop redis)."):
            fourth = await send_message(a, "a", "written while Redis is down")
            durable = await until(
                lambda: any(e["resource_id"] == fourth.get("id") for e in sb.events)
            )
            degraded = await until(lambda: sb.presence is not None and sb.presence["degraded"])
            report.check(
                bool(fourth) and durable,
                "8a. Redis down: the write succeeds and B still receives the event",
            )
            report.check(
                degraded and sb.presence["online"] == [],
                "8b. presence says degraded (unknown), not 'everyone is offline'",
            )
            await operator("start_redis", "Start Redis again (docker compose up -d --wait redis).")
            rebuilt = await until(
                lambda: (
                    sb.presence is not None
                    and not sb.presence["degraded"]
                    and len(sb.online()) == 2
                ),
                40,
            )
            await apply(replica_b, sb, b)
            report.check(rebuilt, "8c. after Redis returns, presence is rebuilt from live sockets")
        else:
            report.skip("8. Redis stopped", "operator skipped")

        # 9. One API process restarts; B reconnects through the other and converges.
        if operator is None:
            report.skip("9. API restart", "run with TF_OPERATOR=1 to perform this step")
        elif await operator("restart_api1", "Restart API process 1 (port 8000) now."):
            await send_message(a, "a", "written during the API restart")
            # A's socket was on the restarted process: it closed (1012/1001: reconnect). Reconnect
            # it from the last event it saw, exactly as a client would after a transient close.
            last_a = max([replica_a.cursor, *(e["room_seq"] for e in sa.events)])
            await sa.close()
            sa = await open_socket("A3", "a", base1, last_a)
            await until(lambda: sa.synced)
            await sb.close()
            sb = await open_socket("B3", "b", base2, replica_b.cursor)
            await until(lambda: sb.synced)
            await asyncio.sleep(0.5)
            await apply(replica_b, sb, b)
            truth = await fresh(b)
            report.check(
                replica_b.cursor == truth["through_seq"]
                and replica_b.messages
                == {m["id"]: (m["version"], m["body"], m["is_deleted"]) for m in truth["messages"]},
                "9. B resumed through the other API process and converged",
            )
        else:
            report.skip("9. API restart", "operator skipped")

        # 10. Removing C closes C's socket 4403 with no frame about it; A is told.
        sc = await open_socket("C1", "c", base1, (await fresh(c))["through_seq"])
        await until(lambda: sc.synced)
        members = (await a.get(f"/rooms/{room}/members")).json()["items"]
        c_id = next(m["user_id"] for m in members if m["role"] == "viewer")
        r = await a.delete(f"/rooms/{room}/members/{c_id}", headers=csrf["a"])
        closed = await until(lambda: sc.done)
        told = await until(lambda: any(e["event_type"] == "member_removed" for e in sa.events))
        report.check(
            r.status_code == 204
            and closed
            and sc.closed == 4403
            and not any(e["event_type"] == "member_removed" for e in sc.events),
            "10a. C removed: C's socket closes 4403 and never gets its own member_removed frame",
            f"close {sc.closed}",
        )
        report.check(told, "10b. A (a member) receives member_removed")
        try:
            await open_socket("C2", "c", base1, 0)
            refused = False
        except InvalidStatus as exc:
            refused = exc.response.status_code == 403
        report.check(
            refused and (await c.get(f"/rooms/{room}/sync")).status_code == 404,
            "10c. C can no longer reconnect (403) or bootstrap (404)",
        )

        # 11. B logs out: B's sockets close 4401 (bounded by the check interval); no reconnect.
        r = await b.post("/auth/logout", headers=csrf["b"])
        closed = await until(lambda: sb.done, 40)
        try:
            await open_socket("B4", "b", base2, 0)
            refused = False
        except InvalidStatus as exc:
            refused = exc.response.status_code == 403
        report.check(
            r.status_code == 204 and closed and sb.closed == 4401 and refused,
            "11. logout: B's idle socket closes 4401 within the check interval; reconnect refused",
            f"close {sb.closed}",
        )
    finally:
        for sock in sockets:
            await sock.close()

    verdict = "ALL CHECKS PASSED" if report.failures == 0 else f"{report.failures} CHECK(S) FAILED"
    print(
        f"\n{verdict}\n(A frame means the server queued it for that socket; it does not prove a "
        "browser rendered it. Reconnection is the client's job; this script plays the client.)"
    )
    return 1 if report.failures else 0


async def _prompt_operator(action: str, instruction: str) -> bool:
    answer = await asyncio.to_thread(
        input, f"\n>>> {instruction}  Press Enter when done (or type 'skip'): "
    )
    return answer.strip().lower() != "skip"


async def _main() -> int:
    cookies = {k: os.environ.get(f"TF_COOKIE_{k.upper()}") for k in "abc"}
    if not all(cookies.values()):
        print(
            "Set TF_COOKIE_A, TF_COOKIE_B and TF_COOKIE_C (see the module docstring).",
            file=sys.stderr,
        )
        return 2
    base1 = os.environ.get("TF_BASE_URL_1", "http://localhost:8000")
    base2 = os.environ.get("TF_BASE_URL_2", "http://localhost:8001")
    name = os.environ.get("TF_COOKIE_NAME", "tf_session")
    origin = os.environ.get("TF_ORIGIN", base1)
    operator = _prompt_operator if os.environ.get("TF_OPERATOR") == "1" else None
    async with contextlib.AsyncExitStack() as stack:
        clients = {
            key: await stack.enter_async_context(
                httpx2.AsyncClient(
                    base_url=base1 if key != "b" else base2,
                    headers={"Cookie": f"{name}={cookies[key]}"},
                )
            )
            for key in "abc"
        }
        return await run(
            clients,
            base1=base1,
            base2=base2,
            cookie_name=name,
            cookies={k: v for k, v in cookies.items() if v},
            origin=origin,
            operator=operator,
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
