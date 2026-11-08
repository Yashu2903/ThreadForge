"""Manual Part 2 verification: two REAL signed-in accounts walk through the room lifecycle.

There is no session-minting command or endpoint (and there must not be): sign in as account A and
as account B through the real Google flow in two separate browser profiles, then copy each
`tf_session` cookie VALUE into an environment variable. Do it in a way that keeps the values out of
your shell history and out of any transcript, for example (PowerShell):

    $env:TF_COOKIE_A = Read-Host "cookie A" -MaskInput
    $env:TF_COOKIE_B = Read-Host "cookie B" -MaskInput

then run from backend/:

    uv run python -m scripts.manual_part2_check

Environment:
    TF_COOKIE_A, TF_COOKIE_B   session cookie values (required; never printed)
    TF_BASE_URL                API origin (default http://localhost:8000)
    TF_COOKIE_NAME             cookie name (default tf_session; production: __Host-tf_session)
    TF_ORIGIN                  Origin header to send; must be in TRUSTED_ORIGINS (default: base url)
    TF_INSPECT_DB=1            also print room/membership/invitation rows via `docker exec psql`
                               (hash PREFIX only, never a raw token)

The script prints PASS/FAIL per step and the room's membership_epoch after each step. It never
prints cookies or invitation tokens. Exit code 1 if any check failed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable

import httpx2

Inspect = Callable[[str, str], Awaitable[None]]  # (step label, room id)


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if not ok:
            self.failures += 1
        suffix = f" - {detail}" if detail else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {label}{suffix}")


async def _csrf(client: httpx2.AsyncClient) -> str:
    response = await client.get("/auth/csrf")
    if response.status_code != 200:
        raise SystemExit(
            f"/auth/csrf returned {response.status_code}: is the cookie valid and the session live?"
        )
    return response.json()["csrf_token"]


def _code(response: httpx2.Response) -> str:
    try:
        return response.json()["error"]["code"]
    except Exception:  # noqa: BLE001 - diagnostic only
        return f"HTTP {response.status_code}"


async def run(
    a: httpx2.AsyncClient,
    b: httpx2.AsyncClient,
    origin: str,
    inspect: Inspect | None = None,
) -> int:
    report = Report()
    headers = {
        "a": {"X-CSRF-Token": await _csrf(a), "Origin": origin},
        "b": {"X-CSRF-Token": await _csrf(b), "Origin": origin},
    }
    room_id = ""

    async def after(step: int, label: str, expected_epoch: int) -> None:
        if not room_id:
            return
        actual = (await a.get(f"/rooms/{room_id}")).json().get("membership_epoch")
        report.check(
            actual == expected_epoch, f"   membership_epoch after step {step}", f"{actual}"
        )
        if inspect:
            await inspect(f"after step {step}: {label}", room_id)

    # 1
    r = await a.post("/rooms", json={"name": "Manual check room"}, headers=headers["a"])
    report.check(r.status_code == 201, "1. A creates a room", f"HTTP {r.status_code}")
    if r.status_code != 201:
        return 1
    room_id = r.json()["id"]
    await after(1, "A created the room", 1)

    # 2
    r = await a.post(f"/rooms/{room_id}/invitations", json={"role": "editor"}, headers=headers["a"])
    report.check(r.status_code == 201 and "token" in r.json(), "2. A creates an editor invitation")
    token = r.json().get("token", "")
    await after(2, "A created an editor invitation", 1)

    # 3
    r = await b.get(f"/rooms/{room_id}")
    report.check(
        r.status_code == 404 and _code(r) == "room_not_found", "3. B cannot read the room", _code(r)
    )
    await after(3, "B was refused", 1)

    # 4
    r = await b.post("/invitations/redeem", json={"token": token}, headers=headers["b"])
    report.check(r.status_code == 200, "4. B redeems the invitation", f"HTTP {r.status_code}")
    await after(4, "B redeemed", 2)

    # 5
    room = await b.get(f"/rooms/{room_id}")
    members = await b.get(f"/rooms/{room_id}/members")
    report.check(
        room.status_code == 200
        and room.json()["role"] == "editor"
        and members.status_code == 200
        and len(members.json()["items"]) == 2,
        "5. B reads the room and its members (role editor, 2 members)",
    )
    await after(5, "B reads", 2)

    # 6
    members_as_a = (await a.get(f"/rooms/{room_id}/members")).json()["items"]
    owner_id = next(m["user_id"] for m in members_as_a if m["role"] == "owner")
    b_id = next(m["user_id"] for m in members_as_a if m["role"] != "owner")
    invite = await b.post(f"/rooms/{room_id}/invitations", json={}, headers=headers["b"])
    remove = await b.delete(f"/rooms/{room_id}/members/{owner_id}", headers=headers["b"])
    report.check(
        (invite.status_code, remove.status_code) == (403, 403),
        "6. B (editor) cannot invite or remove",
        f"{_code(invite)}, {_code(remove)}",
    )
    await after(6, "B refused as editor", 2)

    # 7
    r = await a.patch(
        f"/rooms/{room_id}/members/{b_id}", json={"role": "viewer"}, headers=headers["a"]
    )
    report.check(r.status_code == 200 and r.json()["role"] == "viewer", "7. A changes B to viewer")
    await after(7, "B is a viewer", 3)

    # 8
    r = await a.delete(f"/rooms/{room_id}/members/{b_id}", headers=headers["a"])
    report.check(r.status_code == 204, "8. A removes B", f"HTTP {r.status_code}")
    await after(8, "B removed", 4)

    # 9
    r = await b.get(f"/rooms/{room_id}")
    report.check(r.status_code == 404, "9. B's access is gone", _code(r))
    await after(9, "B refused again", 4)

    # 10
    r = await b.post("/invitations/redeem", json={"token": token}, headers=headers["b"])
    still = await b.get(f"/rooms/{room_id}")
    report.check(
        r.status_code == 404 and _code(r) == "invitation_invalid" and still.status_code == 404,
        "10. the used invitation cannot restore access",
        _code(r),
    )
    await after(10, "old token refused", 4)

    # 11
    fresh = await a.post(
        f"/rooms/{room_id}/invitations", json={"role": "viewer"}, headers=headers["a"]
    )
    r = await b.post(
        "/invitations/redeem", json={"token": fresh.json()["token"]}, headers=headers["b"]
    )
    back = await b.get(f"/rooms/{room_id}")
    report.check(
        r.status_code == 200 and back.status_code == 200 and back.json()["role"] == "viewer",
        "11. a FRESH invitation lets B rejoin (as viewer)",
    )
    await after(11, "B rejoined", 5)

    verdict = "ALL CHECKS PASSED" if report.failures == 0 else f"{report.failures} CHECK(S) FAILED"
    print(f"\n{verdict}")
    return 1 if report.failures else 0


def _docker_inspect(container: str = "threadforge-postgres-1") -> Inspect:
    async def inspect(label: str, room_id: str) -> None:
        print(f"      -- database rows {label}")
        queries = [
            "SELECT id, owner_id, membership_epoch, version FROM rooms WHERE id = '{r}'",
            "SELECT user_id, role, state, joined_at, removed_at FROM memberships "
            "WHERE room_id = '{r}' ORDER BY joined_at",
            "SELECT id, role, left(token_hash, 8) AS hash_prefix, expires_at, used_by, revoked_at "
            "FROM invitations WHERE room_id = '{r}' ORDER BY created_at",
        ]
        for query in queries:
            # fixed argv (no shell); room_id comes from our own API response
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
            ]
            await asyncio.to_thread(subprocess.run, command, check=False)

    return inspect


async def _main() -> int:
    cookie_a, cookie_b = os.environ.get("TF_COOKIE_A"), os.environ.get("TF_COOKIE_B")
    if not cookie_a or not cookie_b:
        print("Set TF_COOKIE_A and TF_COOKIE_B (see the module docstring).", file=sys.stderr)
        return 2
    base = os.environ.get("TF_BASE_URL", "http://localhost:8000")
    name = os.environ.get("TF_COOKIE_NAME", "tf_session")
    origin = os.environ.get("TF_ORIGIN", base)
    async with (
        httpx2.AsyncClient(base_url=base, headers={"Cookie": f"{name}={cookie_a}"}) as a,
        httpx2.AsyncClient(base_url=base, headers={"Cookie": f"{name}={cookie_b}"}) as b,
    ):
        inspect = _docker_inspect() if os.environ.get("TF_INSPECT_DB") == "1" else None
        return await run(a, b, origin, inspect)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
