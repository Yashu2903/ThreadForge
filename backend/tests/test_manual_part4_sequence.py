"""The Part 4 manual demonstration script, run against two in-process API servers (real uvicorn,
real WebSockets, real Redis) with fixture users and an in-process dispatcher loop.

It executes the SAME script a human runs with two real Google accounts
(scripts/manual_part4_check.py), so the script itself is proven correct. It does not prove that
real Google login works, and the Redis-outage step is left to the human (it is reported SKIP here;
test_realtime_failures.py covers Redis outage behaviour separately).
"""

import asyncio
import contextlib

import httpx2
import pytest

from scripts.manual_part4_check import run
from tests.helpers import TRUSTED_ORIGIN
from tests.realtime_helpers import Pump, live_server


@pytest.fixture
def fast(settings):
    """Reconciliation every 0.3 s instead of 5 s, so the demo does not wait on real intervals."""
    return settings.model_copy(
        update={"reconcile_interval_seconds": 0.3, "reconcile_interval_degraded_seconds": 0.2}
    )


class LoopingDispatcher:
    """Stands in for `python -m app.workers.outbox_dispatcher`: flushes the outbox repeatedly,
    and can be stopped/started like an operator would."""

    def __init__(self, pump: Pump) -> None:
        self.pump = pump
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        async def loop() -> None:
            while True:
                await self.pump.flush()
                await asyncio.sleep(0.05)

        self.task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None


async def test_manual_part4_script_passes_against_fixture_users(
    settings, fast, engine, sessionmaker, redis_client, make_actor, capsys
):
    owner, viewer = await make_actor("A"), await make_actor("B")
    dispatcher = LoopingDispatcher(Pump(sessionmaker, settings))
    dispatcher.start()

    async def operator(action: str, instruction: str) -> bool:
        if action == "stop_dispatcher":
            await dispatcher.stop()
        elif action == "start_dispatcher":
            dispatcher.start()
        else:
            return False  # Redis is left to the human
        return True

    try:
        async with live_server(fast, engine) as one, live_server(fast, engine) as two:
            code = await run(
                owner.client,
                viewer.client,
                base1=one.http_url,
                base2=two.http_url,
                cookie_name=settings.session_cookie_name,
                cookie_a=owner.secret,
                cookie_b=viewer.secret,
                origin=TRUSTED_ORIGIN,
                operator=operator,
                quiet_seconds=0.6,
            )
    finally:
        await dispatcher.stop()
        await dispatcher.pump.aclose()
    output = capsys.readouterr().out
    assert code == 0, output
    assert "ALL CHECKS PASSED" in output and "[FAIL]" not in output
    assert "5c. dispatcher back" in output and "[SKIP] 6. Redis stopped" in output
    for secret in (
        owner.secret,
        viewer.secret,
        await owner.csrf_token(),
        await viewer.csrf_token(),
    ):
        assert secret not in output
    assert "hello from A" not in output and httpx2 is not None


async def test_manual_part4_script_skips_operator_steps_without_an_operator(
    settings, fast, engine, sessionmaker, redis_client, make_actor, capsys
):
    owner, viewer = await make_actor("A"), await make_actor("B")
    dispatcher = LoopingDispatcher(Pump(sessionmaker, settings))
    dispatcher.start()
    try:
        async with live_server(fast, engine) as one, live_server(fast, engine) as two:
            code = await run(
                owner.client,
                viewer.client,
                base1=one.http_url,
                base2=two.http_url,
                cookie_name=settings.session_cookie_name,
                cookie_a=owner.secret,
                cookie_b=viewer.secret,
                origin=TRUSTED_ORIGIN,
                quiet_seconds=0.6,
            )
    finally:
        await dispatcher.stop()
        await dispatcher.pump.aclose()
    output = capsys.readouterr().out
    assert code == 0, output
    assert output.count("[SKIP]") == 2 and "[FAIL]" not in output


async def test_manual_part4_script_passes_with_no_dispatcher_because_delivery_is_durable(
    settings, fast, engine, redis_client, make_actor, capsys, monkeypatch
):
    """Part 4's version of this test expected a FAILURE here (no dispatcher, no live delivery).
    Since Part 5, sockets read PostgreSQL in order and reconciliation finds new events, so the
    flow works with no dispatcher process at all (Redis hints only make it faster)."""
    owner, viewer = await make_actor("A"), await make_actor("B")
    async with live_server(fast, engine) as one, live_server(fast, engine) as two:
        code = await run(
            owner.client,
            viewer.client,
            base1=one.http_url,
            base2=two.http_url,
            cookie_name=settings.session_cookie_name,
            cookie_a=owner.secret,
            cookie_b=viewer.secret,
            origin=TRUSTED_ORIGIN,
            quiet_seconds=0.6,
        )
    output = capsys.readouterr().out
    assert code == 0, output
    assert "ALL CHECKS PASSED" in output and "[FAIL]" not in output
