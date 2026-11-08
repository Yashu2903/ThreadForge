"""The Part 5 manual demonstration script, run against two in-process API servers (real uvicorn,
real WebSockets, real PostgreSQL and Redis) with fixture users and an in-process dispatcher loop.

It executes the SAME script a human runs with three real Google accounts
(scripts/manual_part5_check.py). It does not prove that real Google login works; the Redis-stop
and API-restart steps are left to the human (SKIP here; test_ws_access.py, test_ws_presence.py and
test_realtime_failures.py cover those behaviours separately, and the real-process run did them).
"""

import asyncio
import contextlib

import pytest

from scripts.manual_part5_check import run
from tests.helpers import TRUSTED_ORIGIN
from tests.realtime_helpers import Pump, live_server

pytestmark = pytest.mark.asyncio


class LoopingDispatcher:
    def __init__(self, pump):
        self.pump = pump
        self.task = None

    def start(self):
        async def loop():
            while True:
                await self.pump.flush()
                await asyncio.sleep(0.05)

        self.task = asyncio.create_task(loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None


@pytest.fixture
def fast(settings):
    return settings.model_copy(
        update={
            "reconcile_interval_seconds": 0.4,
            "reconcile_interval_degraded_seconds": 0.2,
            "access_check_interval_seconds": 0.6,
            "access_cache_seconds": 0.1,
            "presence_tick_seconds": 0.4,
            "presence_expiry_seconds": 3.0,
            "presence_hint_min_interval_seconds": 0.05,
            "typing_expiry_seconds": 1.0,
            "typing_state_min_interval_seconds": 0.05,
            "client_heartbeat_interval_seconds": 1.0,
            "ws_watchdog_timeout_seconds": 20.0,
            "message_writes_per_minute": 1000,
        }
    )


async def test_manual_part5_script_passes_against_fixture_users(
    settings, fast, engine, sessionmaker, redis_client, make_actor, capsys
):
    users = {k: await make_actor(n) for k, n in (("a", "A"), ("b", "B"), ("c", "C"))}
    tokens = [await user.csrf_token() for user in users.values()]  # before B logs out
    dispatcher = LoopingDispatcher(Pump(sessionmaker, fast))
    dispatcher.start()

    async def operator(action, instruction):
        if action == "stop_dispatcher":
            await dispatcher.stop()
        elif action == "start_dispatcher":
            dispatcher.start()
        else:
            return False
        return True

    try:
        async with live_server(fast, engine) as one, live_server(fast, engine) as two:
            code = await run(
                {k: u.client for k, u in users.items()},
                base1=one.http_url,
                base2=two.http_url,
                cookie_name=settings.session_cookie_name,
                cookies={k: u.secret for k, u in users.items()},
                origin=TRUSTED_ORIGIN,
                operator=operator,
                typing_wait=8.0,
            )
    finally:
        await dispatcher.stop()
        await dispatcher.pump.aclose()
    output = capsys.readouterr().out
    assert code == 0, output
    assert "ALL CHECKS PASSED" in output and "[FAIL]" not in output
    assert "7. with no dispatcher" in output and output.count("[SKIP]") == 2
    for user in users.values():  # no cookie, token or message text is ever printed
        assert user.secret not in output
    assert not any(token in output for token in tokens)
    assert "first message" not in output and "written while B is away" not in output
