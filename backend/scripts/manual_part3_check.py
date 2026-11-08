"""Manual Part 3 demonstration: three REAL signed-in accounts exchange, edit and delete messages.

Accounts: A = owner, B = editor (removed near the end), C = viewer. Chat is open to every
active member, so C can send, edit and delete their own messages like anyone else.

Sign in as each through the real Google flow in three separate browser profiles, then copy each
`tf_session` cookie VALUE into an environment variable without putting it on a command line or
into a transcript (PowerShell):

    $env:TF_COOKIE_A = Read-Host "cookie A" -MaskInput
    $env:TF_COOKIE_B = Read-Host "cookie B" -MaskInput
    $env:TF_COOKIE_C = Read-Host "cookie C" -MaskInput

Start the backend with a small limit so step 11 is quick, e.g. MESSAGE_WRITES_PER_MINUTE=5, then
run from backend/:

    uv run python -m scripts.manual_part3_check

Environment: TF_BASE_URL (default http://localhost:8000), TF_COOKIE_NAME (default tf_session;
production __Host-tf_session), TF_ORIGIN (must be in TRUSTED_ORIGINS; default: base url),
TF_INSPECT_DB=1 to print message/bucket rows via `docker exec psql` (no text, no fingerprint).

Prints PASS/FAIL per step; never prints cookies, invitation tokens or CSRF tokens. NOTE: nothing
here is live synchronization: other clients only see changes by fetching history over HTTP.
Exit code 1 if any check failed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable

import httpx2

Inspect = Callable[[str, str], Awaitable[None]]  # (label, room id)
MAX_SENDS_TO_REACH_LIMIT = 200


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if not ok:
            self.failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")


async def _csrf(client: httpx2.AsyncClient) -> str:
    response = await client.get("/auth/csrf")
    if response.status_code != 200:
        raise SystemExit(f"/auth/csrf returned {response.status_code}: is the cookie valid?")
    return response.json()["csrf_token"]


def _code(response: httpx2.Response) -> str:
    try:
        return response.json()["error"]["code"]
    except Exception:  # noqa: BLE001 - diagnostic only
        return f"HTTP {response.status_code}"


async def run(
    a: httpx2.AsyncClient,
    b: httpx2.AsyncClient,
    c: httpx2.AsyncClient,
    origin: str,
    inspect: Inspect | None = None,
) -> int:
    report = Report()
    headers = {
        name: {"X-CSRF-Token": await _csrf(client), "Origin": origin}
        for name, client in (("a", a), ("b", b), ("c", c))
    }

    # 1. A creates a room and invitations; B joins as editor; C joins as viewer.
    r = await a.post("/rooms", json={"name": "Part 3 demo"}, headers=headers["a"])
    if r.status_code != 201:
        report.check(False, "1. A creates a room", f"HTTP {r.status_code}")
        return 1
    room = r.json()["id"]
    joined = []
    for role, client, key in (("editor", b, "b"), ("viewer", c, "c")):
        inv = await a.post(f"/rooms/{room}/invitations", json={"role": role}, headers=headers["a"])
        redeem = await client.post(
            "/invitations/redeem", json={"token": inv.json()["token"]}, headers=headers[key]
        )
        joined.append((role, redeem.status_code))
    report.check(
        joined == [("editor", 200), ("viewer", 200)],
        "1. A created the room; B joined as editor, C as viewer",
        str(joined),
    )

    async def show(label: str) -> None:
        if inspect:
            await inspect(label, room)

    # 2. B sends a message.
    cmid = str(uuid.uuid4())
    text = "Hello from B (demo)"
    r = await b.post(
        f"/rooms/{room}/messages",
        json={"client_message_id": cmid, "body": text},
        headers=headers["b"],
    )
    ok = r.status_code == 201 and r.json()["version"] == 1
    report.check(ok, "2. B sends a message", f"HTTP {r.status_code}")
    if not ok:
        return 1
    message = r.json()
    await show("after step 2: B sent")

    # 3. A fetches history.
    r = await a.get(f"/rooms/{room}/messages")
    report.check(
        r.status_code == 200 and [m["id"] for m in r.json()["items"]] == [message["id"]],
        "3. A fetches history and sees B's message",
    )

    # 4. B retries the same send: the same message id comes back.
    r = await b.post(
        f"/rooms/{room}/messages",
        json={"client_message_id": cmid, "body": text},
        headers=headers["b"],
    )
    report.check(
        r.status_code == 200 and r.json()["id"] == message["id"],
        "4. B's retry returns the same message (200)",
        f"HTTP {r.status_code}",
    )

    # 5. B reuses the id with different text.
    r = await b.post(
        f"/rooms/{room}/messages",
        json={"client_message_id": cmid, "body": "Different text"},
        headers=headers["b"],
    )
    report.check(
        r.status_code == 409 and _code(r) == "message_id_reused",
        "5. reusing the id with different text is refused",
        _code(r),
    )

    # 6. B edits with the current version, then with the old version.
    r = await b.patch(
        f"/rooms/{room}/messages/{message['id']}",
        json={"body": "Hello from B, edited", "expected_version": 1},
        headers=headers["b"],
    )
    report.check(
        r.status_code == 200 and r.json()["version"] == 2,
        "6a. B edits with the current version (now v2)",
        f"HTTP {r.status_code}",
    )
    r = await b.patch(
        f"/rooms/{room}/messages/{message['id']}",
        json={"body": "From a stale tab", "expected_version": 1},
        headers=headers["b"],
    )
    report.check(
        r.status_code == 409
        and _code(r) == "message_version_conflict"
        and r.json()["error"].get("current_version") == 2,
        "6b. an edit with the old version conflicts (current_version 2)",
        _code(r),
    )
    await show("after step 6: edited")

    # 7. A (owner) deletes B's message.
    r = await a.delete(
        f"/rooms/{room}/messages/{message['id']}",
        params={"expected_version": 2},
        headers=headers["a"],
    )
    report.check(r.status_code == 204, "7. A deletes B's message", f"HTTP {r.status_code}")

    # 8. History shows a tombstone; retrying the original send does not restore the text.
    history = await a.get(f"/rooms/{room}/messages")
    items = history.json()["items"] if history.status_code == 200 else []
    tomb = items[0] if items else {}
    report.check(
        bool(tomb) and tomb["is_deleted"] and tomb["body"] is None,
        "8a. history shows a tombstone with no text",
    )
    r = await b.post(
        f"/rooms/{room}/messages",
        json={"client_message_id": cmid, "body": text},
        headers=headers["b"],
    )
    report.check(
        r.status_code == 200 and r.json()["body"] is None and r.json()["is_deleted"],
        "8b. retrying the original send returns the tombstone (text not restored)",
    )
    await show("after step 8: deleted")

    # 9. C (viewer) CAN chat: sends successfully. Role only limits documents/uploads (later parts).
    #    C may only touch their own messages: editing or deleting B's gets 403 message_not_author.
    live = await b.post(
        f"/rooms/{room}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": "A live message from B"},
        headers=headers["b"],
    )
    b_live = live.json() if live.status_code == 201 else {"id": str(uuid.uuid4()), "version": 1}
    r = await c.post(
        f"/rooms/{room}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": "Hello from the viewer"},
        headers=headers["c"],
    )
    report.check(
        live.status_code == 201 and r.status_code == 201 and r.json()["version"] == 1,
        "9a. C (viewer) sends a message successfully",
        f"HTTP {r.status_code}",
    )
    edit_other = await c.patch(
        f"/rooms/{room}/messages/{b_live['id']}",
        json={"body": "C rewriting B", "expected_version": b_live["version"]},
        headers=headers["c"],
    )
    delete_other = await c.delete(
        f"/rooms/{room}/messages/{b_live['id']}",
        params={"expected_version": b_live["version"]},
        headers=headers["c"],
    )
    report.check(
        (edit_other.status_code, delete_other.status_code) == (403, 403)
        and _code(edit_other) == _code(delete_other) == "message_not_author",
        "9b. C cannot edit or delete B's message (403 message_not_author)",
        f"{_code(edit_other)}, {_code(delete_other)}",
    )

    # 10. A removes B; B can no longer read or write.
    members = (await a.get(f"/rooms/{room}/members")).json()["items"]
    b_id = next(m["user_id"] for m in members if m["role"] == "editor")
    r = await a.delete(f"/rooms/{room}/members/{b_id}", headers=headers["a"])
    read = await b.get(f"/rooms/{room}/messages")
    write = await b.post(
        f"/rooms/{room}/messages",
        json={"client_message_id": str(uuid.uuid4()), "body": "still here?"},
        headers=headers["b"],
    )
    report.check(
        r.status_code == 204 and read.status_code == 404 and write.status_code == 404,
        "10. A removes B; B can no longer read or write (404)",
        f"{read.status_code}, {write.status_code}",
    )

    # 11. Exceeding the write limit returns 429 with Retry-After.
    limited = None
    for i in range(MAX_SENDS_TO_REACH_LIMIT):
        r = await a.post(
            f"/rooms/{room}/messages",
            json={"client_message_id": str(uuid.uuid4()), "body": f"limit probe {i}"},
            headers=headers["a"],
        )
        if r.status_code == 429:
            limited = (i, r)
            break
    ok = (
        limited is not None
        and _code(limited[1]) == "rate_limited"
        and limited[1].headers.get("retry-after", "").isdigit()
    )
    detail = (
        f"after {limited[0]} sends, Retry-After={limited[1].headers.get('retry-after')}s"
        if limited
        else "never limited: start the backend with MESSAGE_WRITES_PER_MINUTE=5"
    )
    report.check(ok, "11. exceeding the write limit returns 429 with Retry-After", detail)
    await show("after step 11: rate limited")

    verdict = "ALL CHECKS PASSED" if report.failures == 0 else f"{report.failures} CHECK(S) FAILED"
    print(
        f"\n{verdict}\n(Other clients see changes only by fetching history: nothing here is live.)"
    )
    return 1 if report.failures else 0


def _docker_inspect(container: str = "threadforge-postgres-1") -> Inspect:
    async def inspect(label: str, room_id: str) -> None:
        print(f"      -- database rows {label}")
        queries = [
            "SELECT id, author_id, version, body IS NULL AS tombstone, "
            "char_length(body) AS chars, "
            "octet_length(initial_request_fingerprint) AS fp_bytes, "
            "created_at, edited_at, deleted_at "
            "FROM messages WHERE room_id = '{r}' ORDER BY created_at",
            "SELECT user_id, window_start, count FROM message_write_buckets "
            "WHERE room_id = '{r}' ORDER BY window_start",
        ]
        for query in queries:
            command = [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "threadforge",
                "-d",
                "threadforge",
                "-c",
                query.format(r=room_id),
            ]  # fixed argv, no shell; room_id comes from our own API response
            await asyncio.to_thread(subprocess.run, command, check=False)

    return inspect


async def _main() -> int:
    cookies = [os.environ.get(f"TF_COOKIE_{k}") for k in "ABC"]
    if not all(cookies):
        print(
            "Set TF_COOKIE_A, TF_COOKIE_B and TF_COOKIE_C (see the module docstring).",
            file=sys.stderr,
        )
        return 2
    base = os.environ.get("TF_BASE_URL", "http://localhost:8000")
    name = os.environ.get("TF_COOKIE_NAME", "tf_session")
    origin = os.environ.get("TF_ORIGIN", base)
    async with (
        httpx2.AsyncClient(base_url=base, headers={"Cookie": f"{name}={cookies[0]}"}) as a,
        httpx2.AsyncClient(base_url=base, headers={"Cookie": f"{name}={cookies[1]}"}) as b,
        httpx2.AsyncClient(base_url=base, headers={"Cookie": f"{name}={cookies[2]}"}) as c,
    ):
        inspect = _docker_inspect() if os.environ.get("TF_INSPECT_DB") == "1" else None
        return await run(a, b, c, origin, inspect)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
