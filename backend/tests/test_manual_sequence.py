"""The manual verification script, run against the in-process app with fixture users.

This executes the SAME script a human runs against real Google accounts
(scripts/manual_part2_check.py), so the script itself is proven correct. It does NOT prove that
real Google login works: users here come from direct fixtures, with sessions made by Part 1's
create_session().
"""

from scripts.manual_part2_check import run
from tests.helpers import TRUSTED_ORIGIN, rows


async def test_manual_check_script_passes_against_fixture_users(alice, bob, sessionmaker, capsys):
    inspected = []

    async def inspect(label, room_id):
        inspected.append((label, room_id))

    exit_code = await run(alice.client, bob.client, TRUSTED_ORIGIN, inspect)
    output = capsys.readouterr().out
    assert exit_code == 0, output
    assert "ALL CHECKS PASSED" in output and "[FAIL]" not in output
    assert len(inspected) == 11  # a database inspection point after every step

    # The script never prints a raw invitation token (only its own wording about them) or cookie.
    [row] = await rows(sessionmaker, "SELECT count(*) AS n FROM invitations")
    assert row["n"] == 2
    for cookie in (alice.client.cookies.get("tf_session"), bob.client.cookies.get("tf_session")):
        assert cookie and cookie not in output


async def test_manual_script_reports_failure_with_a_nonzero_exit(alice, bob, capsys):
    # A wrong Origin makes every mutation fail CSRF, so the script must NOT claim success.
    exit_code = await run(alice.client, bob.client, "https://wrong-origin.example")
    assert exit_code == 1
    assert "[FAIL]" in capsys.readouterr().out
