"""The test helpers wait out a backward step of the database clock (and nothing else).

The Postgres server behind Docker Desktop steps its clock back by about 125-190 ms every 30 s (docs/ai.md 23.3). Work is stamped
`run_after = now()` and claimed when `run_after <= now()`, so work created just before a step is not due for a moment and a drain that
runs straight away used to return nothing. The step is simulated here by moving `run_after` (and `created_at`) a little into the
future, exactly what the step does to a row that was due when it was created.
"""

import time

import pytest

from tests.analysis_helpers import answer, block, build_world, wait_for_clock_step
from tests.helpers import execute, rows

OPS = b"# Ops\n\nThe warehouse processed twelve thousand orders in March.\n"


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def queued_source(world):
    source = await world.upload(world.owner, "ops.md", OPS)
    await world.analyze(world.owner, source)
    return source


def _ahead(seconds: float) -> str:
    return f"clock_timestamp() + interval '{seconds} seconds'"


async def test_an_analysis_job_that_is_only_waiting_on_the_clock_is_drained(world):
    await queued_source(world)
    await execute(
        world.sm,
        f"UPDATE analysis_jobs SET created_at = t.t, run_after = t.t FROM (SELECT {_ahead(0.4)} AS t) t",
    )
    started = time.monotonic()
    assert await world.run_worker() == ["succeeded"]
    assert (
        time.monotonic() - started >= 0.3
    )  # it really waited for the clock, it did not skip the condition


async def test_work_delayed_on_purpose_is_not_waited_for(world):
    await queued_source(world)
    # a retry backoff: run_after is later than created_at (the row was NOT due when it was created)
    await execute(world.sm, "UPDATE analysis_jobs SET run_after = now() + interval '5 seconds'")
    started = time.monotonic()
    assert await world.run_worker() == []
    assert time.monotonic() - started < 1.0
    assert (await rows(world.sm, "SELECT state FROM analysis_jobs"))[0]["state"] == "queued"


async def test_the_wait_is_bounded(world):
    await queued_source(world)
    # due when created, but far beyond any clock step: not something to wait for
    await execute(
        world.sm,
        f"UPDATE analysis_jobs SET created_at = t.t, run_after = t.t FROM (SELECT {_ahead(30)} AS t) t",
    )
    started = time.monotonic()
    assert await world.run_worker() == []
    assert time.monotonic() - started < 1.0


async def test_a_request_that_is_only_waiting_on_the_clock_is_drained(world):
    source = await world.upload(world.owner, "ops.md", OPS)
    await world.analyze(world.owner, source)
    assert set(await world.run_worker()) == {"succeeded"}
    world.stub.answer_fn = lambda p: answer(block("Twelve thousand orders were processed.", "P1"))
    r = await world.send(world.owner, "How many orders?", sources=[source], task_hint="ask")
    assert r.status_code == 201, r.text
    await execute(
        world.sm,
        f"UPDATE ai_requests SET created_at = t.t, run_after = t.t FROM (SELECT {_ahead(0.4)} AS t) t",
    )
    started = time.monotonic()
    assert await world.drain() == ["request:published"]
    assert time.monotonic() - started >= 0.3


async def test_a_request_delayed_on_purpose_is_not_waited_for(world):
    source = await world.upload(world.owner, "ops.md", OPS)
    await world.analyze(world.owner, source)
    assert set(await world.run_worker()) == {"succeeded"}
    r = await world.send(world.owner, "How many orders?", sources=[source], task_hint="ask")
    assert r.status_code == 201, r.text
    await execute(world.sm, "UPDATE ai_requests SET run_after = now() + interval '5 seconds'")
    started = time.monotonic()
    assert await world.drain() == []
    assert time.monotonic() - started < 1.0


async def test_work_that_became_due_a_moment_ago_is_tried_again(world):
    """A claim compares with `now()` from the START of its transaction. Work that became due a few ms later is missed by that
    claim, and a check made afterwards finds it already due: that must still count as 'try again'."""
    await queued_source(world)
    await execute(
        world.sm,
        f"UPDATE analysis_jobs SET created_at = t.t, run_after = t.t FROM (SELECT {_ahead(-0.05)} AS t) t",
    )
    assert await wait_for_clock_step(world.sm) is True
    assert await world.run_worker() == ["succeeded"]


async def test_work_that_has_long_been_due_is_not_retried_forever(world):
    await queued_source(world)
    await execute(
        world.sm,
        f"UPDATE analysis_jobs SET created_at = t.t, run_after = t.t FROM (SELECT {_ahead(-10)} AS t) t",
    )
    assert await wait_for_clock_step(world.sm) is False


async def test_cancelled_work_is_not_waited_for(world):
    await queued_source(world)
    await execute(
        world.sm,
        f"UPDATE analysis_jobs SET created_at = t.t, run_after = t.t, cancel_requested_at = now() FROM (SELECT {_ahead(0.4)} AS t) t",
    )
    assert await wait_for_clock_step(world.sm) is False


def test_the_helpers_use_the_same_comparison_as_the_claims():
    """The wait must follow the claims' own rule (`run_after <= now()` on queued work): if a claim changed, so must the helper."""
    import inspect

    from app.ai.analysis import jobs
    from app.services import ai_requests

    assert "j.run_after <= now()" in inspect.getsource(jobs)
    assert "r.run_after <= now()" in inspect.getsource(ai_requests)
