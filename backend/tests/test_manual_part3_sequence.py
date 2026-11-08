"""The Part 3 manual demonstration script, run against the in-process app with fixture users.

It executes the SAME script a human runs with three real Google accounts
(scripts/manual_part3_check.py), so the script itself is proven correct. It does not prove that
real Google login works: users here come from direct fixtures with sessions made by Part 1's
create_session(). Nothing in it is live synchronization; it only fetches over HTTP.
"""

import asyncio

import pytest_asyncio

from scripts.manual_part3_check import run
from tests.factories import make_settings
from tests.helpers import TRUSTED_ORIGIN, scalar


@pytest_asyncio.fixture(autouse=True)
async def _start_early_in_the_minute(sessionmaker):
    while True:
        left = float(
            await scalar(
                sessionmaker,
                "SELECT extract(epoch FROM date_trunc('minute', clock_timestamp())"
                " + interval '1 minute' - clock_timestamp())",
            )
        )
        if left >= 10:
            return
        await asyncio.sleep(left + 0.3)


async def test_manual_part3_script_passes_against_fixture_users(make_actor, capsys):
    demo = make_settings(message_writes_per_minute=5)  # a small limit, as the instructions say
    owner, editor, viewer = [await make_actor(n, app_settings=demo) for n in ("A", "B", "C")]
    inspected = []

    async def inspect(label, room_id):
        inspected.append(label)

    code = await run(owner.client, editor.client, viewer.client, TRUSTED_ORIGIN, inspect)
    output = capsys.readouterr().out
    assert code == 0, output
    assert "ALL CHECKS PASSED" in output and "[FAIL]" not in output
    assert len(inspected) >= 4  # database inspection points were reached
    for actor in (owner, editor, viewer):  # no cookie, token or CSRF value is ever printed
        assert actor.client.cookies.get("tf_session") not in output
        assert await actor.csrf_token() not in output


async def test_manual_part3_script_fails_loudly_when_the_limit_is_never_reached(make_actor, capsys):
    """If the limit is too high to reach in the script's 200 probes, the script must say so and
    fail rather than claim success."""
    huge = make_settings(message_writes_per_minute=100_000)
    owner, editor, viewer = [await make_actor(n, app_settings=huge) for n in ("A", "B", "C")]
    code = await run(owner.client, editor.client, viewer.client, TRUSTED_ORIGIN)
    output = capsys.readouterr().out
    assert code == 1 and "never limited" in output and "[FAIL]" in output
