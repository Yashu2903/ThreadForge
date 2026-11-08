"""Manual Part 4 demonstration: live room events across TWO API processes.

Two REAL signed-in accounts (A = owner, B = viewer) hold WebSockets on two different API
processes while the outbox dispatcher publishes through Redis. Sign in as each through the real
Google flow in two browser profiles and copy each `tf_session` cookie VALUE into an environment
variable without putting it on a command line or into a transcript (PowerShell):

    $env:TF_COOKIE_A = Read-Host "cookie A" -MaskInput
    $env:TF_COOKIE_B = Read-Host "cookie B" -MaskInput

Start everything (see README "Part 4"): Postgres + Redis (docker compose up -d --wait), then

    uv run uvicorn app.main:create_app --factory --port 8000 --no-access-log --no-proxy-headers `
        --ws-max-size 1024
    uv run uvicorn app.main:create_app --factory --port 8001 --no-access-log --no-proxy-headers `
        --ws-max-size 1024
    uv run python -m app.workers.outbox_dispatcher

and run from backend/:

    uv run python -m scripts.manual_part4_check

Environment: TF_BASE_URL_1 (default http://localhost:8000), TF_BASE_URL_2 (default
http://localhost:8001), TF_COOKIE_NAME (default tf_session; production __Host-tf_session),
TF_ORIGIN (must be in TRUSTED_ORIGINS; default: base url 1), TF_OPERATOR=1 to be prompted for the
steps that need you to stop and start the dispatcher or Redis (otherwise they are SKIPPED).

Prints PASS/FAIL/SKIP per step; never prints cookies, invitation tokens, CSRF tokens or message
text. NOTE: a frame proves the server QUEUED it for that socket, never that a browser rendered it,
and the script does not test reconnection or replay (Part 5). Exit code 1 if any check failed.
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
from websockets.exceptions import ConnectionClosed, InvalidStatus

# (action, instruction) -> True if the step was performed. Actions: stop_dispatcher,
# start_dispatcher, stop_redis, start_redis.
Operator = Callable[[str, str], Awaitable[bool]]
FRAME_WAIT_SECONDS = 15.0


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if not ok:
            self.failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")

    def skip(self, label: str, why: str) -> None:
        print(f"[SKIP] {label} - {why}")


async def _csrf(client: httpx2.AsyncClient) -> str:
    response = await client.get("/auth/csrf")
    if response.status_code != 200:
        raise SystemExit(f"/auth/csrf returned {response.status_code}: is the cookie valid?")
    return response.json()["csrf_token"]


def _ws_url(base: str, room_id: str) -> str:
    return base.replace("http", "ws", 1) + f"/rooms/{room_id}/ws"


async def _connect(base: str, room_id: str, name: str, cookie: str, origin: str | None):
    headers = {"Cookie": f"{name}={cookie}"}
    if origin is not None:
        headers["Origin"] = origin
    return await connect(
        _ws_url(base, room_id),
        additional_headers=headers,
        origin=None,
        open_timeout=10,
        ping_interval=None,
    )


async def _next(ws: ClientConnection, within: float | None = None) -> dict[str, Any] | None:
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=within or FRAME_WAIT_SECONDS))
    except (TimeoutError, ConnectionClosed):
        return None


async def _close_code(ws: ClientConnection, within: float | None = None) -> tuple[list, int | None]:
    frames: list[dict[str, Any]] = []
    try:
        while True:
            frames.append(
                json.loads(await asyncio.wait_for(ws.recv(), timeout=within or FRAME_WAIT_SECONDS))
            )
    except ConnectionClosed as exc:
        return frames, exc.rcvd.code if exc.rcvd else None
    except TimeoutError:
        return frames, None


async def _silent(ws: ClientConnection, seconds: float) -> bool:
    try:
        await asyncio.wait_for(ws.recv(), timeout=seconds)
    except TimeoutError:
        return True
    except ConnectionClosed:
        return False
    return False


async def run(  # noqa: PLR0915 - a linear script reads best top to bottom
    a: httpx2.AsyncClient,
    b: httpx2.AsyncClient,
    *,
    base1: str,
    base2: str,
    cookie_name: str,
    cookie_a: str,
    cookie_b: str,
    origin: str,
    operator: Operator | None = None,
    quiet_seconds: float = 2.0,
) -> int:
    report = Report()
    csrf_a = {"X-CSRF-Token": await _csrf(a), "Origin": origin}
    csrf_b = {"X-CSRF-Token": await _csrf(b), "Origin": origin}

    # 1. A creates a room; B joins as a viewer (chat is open to every active member).
    r = await a.post("/rooms", json={"name": "Part 4 demo"}, headers=csrf_a)
    if r.status_code != 201:
        report.check(False, "1. A creates a room", f"HTTP {r.status_code}")
        return 1
    room = r.json()["id"]
    inv = await a.post(f"/rooms/{room}/invitations", json={"role": "viewer"}, headers=csrf_a)
    joined = await b.post(
        "/invitations/redeem", json={"token": inv.json()["token"]}, headers=csrf_b
    )
    report.check(joined.status_code == 200, "1. A created the room; B joined as viewer")

    # 2. Each user opens a socket on a DIFFERENT API process and resumes from the room's seq.
    history = (await a.get(f"/rooms/{room}/events")).json()
    ws_a = await _connect(base1, room, cookie_name, cookie_a, origin)
    ws_b = await _connect(base2, room, cookie_name, cookie_b, origin)
    resume = json.dumps(
        {"type": "resume", "schema_version": 1, "after_seq": history["current_room_seq"]}
    )
    await ws_a.send(resume)
    await ws_b.send(resume)
    done = []
    for ws in (ws_a, ws_b):
        frames = [await _next(ws), await _next(ws)]
        done.append(frames)
    ok = all(
        f[0] is not None
        and f[1] is not None
        and [x["type"] for x in f] == ["sync_started", "sync_complete"]
        and f[1]["through_seq"] == history["current_room_seq"]
        for f in done
    )
    report.check(ok, "2. A (API 1) and B (API 2) are connected and synchronized to the same seq")
    if not ok:
        return 1

    # The dispatcher may still be publishing the setup events (room_created, member_joined), or may
    # already have published them before the sockets opened. Drain whatever arrives so the steps
    # below look only at what they cause.
    setup = history["current_room_seq"]
    for ws in (ws_a, ws_b):
        while await _next(ws, quiet_seconds) is not None:
            pass

    # 3. A sends a message over HTTP; both sockets receive the SAME event through Redis.
    r = await a.post(
        f"/rooms/{room}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": "hello from A"},
        headers=csrf_a,
    )
    sent_ok = r.status_code == 201
    fa, fb = await _next(ws_a), await _next(ws_b)
    ok = (
        sent_ok
        and bool(fa and fb)
        and fa["event_id"] == fb["event_id"]
        and fa["event_type"] == fb["event_type"] == "message_created"
    )
    report.check(
        ok,
        "3. one message, one event: both sockets (two processes) receive message_created",
        "" if ok else "no frame in time: is the outbox dispatcher running?",
    )
    report.check(
        bool(fa) and "hello from A" not in json.dumps(fa),
        "3b. the frame carries ids and versions, never the message text",
    )

    # 4. The event is also in durable history (what a client would recover from).
    events = (await b.get(f"/rooms/{room}/events?after_seq={setup}")).json()
    report.check(
        bool(fa) and [e["event_id"] for e in events["items"]] == [fa["event_id"]],
        "4. the same event is in B's durable history",
    )

    # 5. Dispatcher stopped: the write succeeds and is durable. Since Part 5 the sockets still get
    #    the event, without any hint: reconciliation (one query per RECONCILE_INTERVAL) notices
    #    that the room moved on, and each socket reads the event from PostgreSQL in order.
    if operator is None:
        report.skip("5. dispatcher paused", "run with TF_OPERATOR=1 to perform this step")
    elif await operator("stop_dispatcher", "Stop the outbox dispatcher now."):
        r = await a.post(
            f"/rooms/{room}/messages",
            json={"client_message_id": str(uuid.uuid4()), "body": "while paused"},
            headers=csrf_a,
        )
        cursor = fa["room_seq"] if fa else 0
        pending = (await a.get(f"/rooms/{room}/events?after_seq={cursor}")).json()
        report.check(
            r.status_code == 201 and len(pending["items"]) == 1,
            "5a. dispatcher stopped: the write succeeds (201) and the event is durable in history",
        )
        fa2, fb2 = await _next(ws_a), await _next(ws_b)
        report.check(
            bool(fa2 and fb2)
            and fa2["event_id"] == fb2["event_id"] == pending["items"][0]["event_id"],
            "5b. with no hint at all, reconciliation delivers it to both sockets",
            "" if fa2 and fb2 else "no frame within the wait: check RECONCILE_INTERVAL_SECONDS",
        )
        await operator("start_dispatcher", "Start the outbox dispatcher again.")
        report.check(
            await _silent(ws_a, quiet_seconds),
            "5c. dispatcher back: the late hint repeats nothing (no duplicate frame)",
        )
    else:
        report.skip("5. dispatcher paused", "operator skipped")

    # 6. Redis stopped: HTTP keeps working, readiness stays healthy, sockets stay open but silent.
    if operator is None:
        report.skip("6. Redis stopped", "run with TF_OPERATOR=1 to perform this step")
    elif await operator("stop_redis", "Stop Redis now (docker compose stop redis)."):
        r = await a.post(
            f"/rooms/{room}/messages",
            json={"client_message_id": str(uuid.uuid4()), "body": "during outage"},
            headers=csrf_a,
        )
        ready = await a.get("/health/ready")
        quiet = await _silent(ws_a, quiet_seconds)
        report.check(
            r.status_code == 201 and ready.status_code == 200 and quiet,
            "6. Redis down: writes succeed, /health/ready is 200, the socket stays open and silent",
            f"HTTP {r.status_code}, ready {ready.status_code}",
        )
        await operator("start_redis", "Start Redis again (docker compose start redis).")
        report.skip(
            "6b. the outage-time event reaching the socket after Redis returns",
            "the dispatcher publishes it only if its retry lands after the API processes "
            "resubscribe; a missed notification is recovered from history, not replayed (Part 5)",
        )
    else:
        report.skip("6. Redis stopped", "operator skipped")

    # 7. An unknown client frame is a protocol violation: close 4400.
    ws_probe = await _connect(base1, room, cookie_name, cookie_a, origin)
    await ws_probe.send(resume)
    await _next(ws_probe)  # sync_started
    await ws_probe.send("hello?")
    _, code = await _close_code(ws_probe)
    report.check(code == 4400, "7. an unknown client frame is refused (close 4400)", str(code))

    # 8. Handshake refusals are all the same bare HTTP 403.
    statuses = []
    for label, cookie, hdr_origin in (
        ("wrong origin", cookie_a, "http://evil.example"),
        ("no origin", cookie_a, None),
        ("no cookie", "", origin),
    ):
        try:
            probe = await _connect(base1, room, cookie_name, cookie, hdr_origin)
            await probe.close()
            statuses.append((label, "connected"))
        except InvalidStatus as exc:
            statuses.append((label, exc.response.status_code))
    report.check(
        all(code == 403 for _, code in statuses),
        "8. bad origin / no origin / no cookie: HTTP 403",
        str(statuses),
    )

    # 9. Removing B: B's socket is closed 4403 WITHOUT a frame about its own removal; A is told.
    #    First drain stragglers (e.g. an event delayed by the outage above): B is still a member
    #    until the removal, so such a frame is legitimate and says nothing about the removal.
    for ws in (ws_a, ws_b):
        while await _next(ws, quiet_seconds) is not None:
            pass
    members = (await a.get(f"/rooms/{room}/members")).json()["items"]
    b_id = next(m["user_id"] for m in members if m["role"] == "viewer")
    r = await a.delete(f"/rooms/{room}/members/{b_id}", headers=csrf_a)
    frames_b, code_b = await _close_code(ws_b)
    frame_a = await _next(ws_a)
    report.check(
        r.status_code == 204
        and code_b == 4403
        and all(f.get("event_type") != "member_removed" for f in frames_b),
        "9a. B removed: B's socket closes 4403 and never gets its own member_removed frame",
        f"close {code_b}, frames {len(frames_b)}",
    )
    report.check(
        bool(frame_a) and frame_a["event_type"] == "member_removed",
        "9b. A (still a member) receives the member_removed event",
    )
    history_b = await b.get(f"/rooms/{room}/events")
    try:
        retry = await _connect(base2, room, cookie_name, cookie_b, origin)
        await retry.close()
        retry_status: object = "connected"
    except InvalidStatus as exc:
        retry_status = exc.response.status_code
    report.check(
        history_b.status_code == 404 and retry_status == 403,
        "9c. B can no longer read history (404) or reconnect (403)",
        f"{history_b.status_code}, {retry_status}",
    )

    for ws in (ws_a, ws_b):
        with contextlib.suppress(Exception):
            await ws.close()
    verdict = "ALL CHECKS PASSED" if report.failures == 0 else f"{report.failures} CHECK(S) FAILED"
    print(
        f"\n{verdict}\n(A frame means the server queued it for that socket; it does not prove a "
        "browser rendered it. Reconnection and replay are Part 5.)"
    )
    return 1 if report.failures else 0


async def _prompt_operator(action: str, instruction: str) -> bool:
    answer = await asyncio.to_thread(
        input, f"\n>>> {instruction}  Press Enter when done (or type 'skip'): "
    )
    return answer.strip().lower() != "skip"


async def _main() -> int:
    cookie_a, cookie_b = os.environ.get("TF_COOKIE_A"), os.environ.get("TF_COOKIE_B")
    if not (cookie_a and cookie_b):
        print("Set TF_COOKIE_A and TF_COOKIE_B (see the module docstring).", file=sys.stderr)
        return 2
    base1 = os.environ.get("TF_BASE_URL_1", "http://localhost:8000")
    base2 = os.environ.get("TF_BASE_URL_2", "http://localhost:8001")
    name = os.environ.get("TF_COOKIE_NAME", "tf_session")
    origin = os.environ.get("TF_ORIGIN", base1)
    operator = _prompt_operator if os.environ.get("TF_OPERATOR") == "1" else None
    async with (
        httpx2.AsyncClient(base_url=base1, headers={"Cookie": f"{name}={cookie_a}"}) as a,
        httpx2.AsyncClient(base_url=base2, headers={"Cookie": f"{name}={cookie_b}"}) as b,
    ):
        return await run(
            a,
            b,
            base1=base1,
            base2=base2,
            cookie_name=name,
            cookie_a=cookie_a,
            cookie_b=cookie_b,
            origin=origin,
            operator=operator,
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
