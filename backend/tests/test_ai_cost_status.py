"""Cost status in the application: a call that used tokens but could not be priced is UNKNOWN, never zero (docs/ai.md 22.7).

Covers the helper, the usage a published result carries, and the usage totals the owner's page reads. Known zero (nothing
used), unpriced (usage recorded, no rate), partial (some priced) and estimated (priced from the configured rates) stay distinct.
"""

from __future__ import annotations

import pytest

from app.ai.pricing import Prices, cost_report
from app.ai.usage import usage_for_room
from tests.ai_helpers import ai_settings
from tests.analysis_helpers import build_world
from tests.helpers import execute, rows

NO_PRICES = dict(
    ai_llm_input_micros_per_mtok=None,
    ai_llm_output_micros_per_mtok=None,
    ai_embed_text_micros_per_mtok=None,
    ai_embed_image_micros_per_mtok=None,
    ai_prices_checked_on="",
)
NO_EMBED_PRICE = dict(ai_embed_text_micros_per_mtok=None, ai_embed_image_micros_per_mtok=None)


def report(settings, **usage):
    args = {"llm_in": 0, "llm_out": 0, "embed_tokens": 0, **usage}
    return cost_report(Prices.of(settings), **args)


def test_nothing_used_is_a_known_zero_whatever_the_pricing():
    for settings in (ai_settings(), ai_settings(**NO_PRICES)):
        r = report(settings)
        assert (r["cost_status"], r["est_cost_micros"], r["unpriced_components"]) == ("none", 0, [])


def test_usage_without_a_rate_is_unknown_not_zero():
    r = report(ai_settings(**NO_PRICES), llm_in=120, llm_out=30, embed_tokens=500)
    assert r["cost_status"] == "unpriced" and r["est_cost_micros"] is None
    assert r["unpriced_components"] == ["generation", "embedding"]


def test_priced_usage_is_estimated_from_the_configured_rates():
    r = report(ai_settings(), llm_in=1_000_000, llm_out=500_000, embed_tokens=1_000_000)
    assert r["cost_status"] == "estimated" and r["unpriced_components"] == []
    assert (
        r["est_cost_micros"] == 1_000_000 + 1_000_000 + 200_000
    )  # $1/M in, $2/M out, $0.2/M embedding


def test_a_partly_priced_result_is_marked_partial_and_is_the_priced_part_only():
    r = report(ai_settings(**NO_EMBED_PRICE), llm_in=1_000_000, llm_out=0, embed_tokens=1_000_000)
    assert r["cost_status"] == "partial" and r["unpriced_components"] == ["embedding"]
    assert r["est_cost_micros"] == 1_000_000  # the generation part; not a total


OPS = b"# Operations\n\nThe warehouse processed twelve thousand orders in March.\n"


async def published_usage(make_actor, sessionmaker, tmp_path, redis_prefix, **settings):
    world = await build_world(make_actor, sessionmaker, tmp_path, redis_prefix, **settings)
    source = await world.upload(world.owner, "ops.md", OPS)
    assert (await world.analyze(world.owner, source)).status_code == 202
    assert set(await world.run_worker()) == {"succeeded"}
    r = await world.send(world.owner, "How many orders?", sources=[source], task_hint="ask")
    await world.drain()
    got = await rows(
        world.sm, "SELECT usage FROM ai_results WHERE request_id = :r", r=r.json()["id"]
    )
    return got[0]["usage"]


async def test_a_published_result_without_prices_says_unpriced_not_zero(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    usage = await published_usage(make_actor, sessionmaker, tmp_path, redis_prefix, **NO_PRICES)
    assert usage["cost_status"] == "unpriced" and usage["est_cost_micros"] is None
    assert usage["llm_in"] > 0  # the usage itself is kept
    assert set(usage["unpriced_components"]) >= {"generation"}


async def test_a_published_result_with_prices_is_estimated(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    usage = await published_usage(make_actor, sessionmaker, tmp_path, redis_prefix)
    assert usage["cost_status"] == "estimated" and usage["est_cost_micros"] > 0
    assert usage["unpriced_components"] == []


@pytest.fixture
async def room(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def ledger_row(world, cost, tokens=10, status="ok"):
    await execute(
        world.sm,
        "INSERT INTO ai_usage_ledger (user_id, charged_user_id, room_id, provider, model, operation, input_tokens, "
        "output_tokens, est_cost_micros, key_source, status) VALUES (:u, :u, :r, 'openai', 'm', 'answer', :t, 0, :c, "
        "'server', :s)",
        u=world.owner.id,
        r=world.room_id,
        t=tokens,
        c=cost,
        s=status,
    )


async def usage(world):
    async with world.sm() as db:
        return await usage_for_room(db, ai_settings(), world.room_id, 30)


async def test_usage_totals_say_none_when_nothing_was_used(room):
    assert (await usage(room))["cost_status"] == "none"


async def test_usage_totals_say_estimated_when_every_call_was_priced(room):
    await ledger_row(room, cost=50)
    await ledger_row(room, cost=70)
    got = await usage(room)
    assert got["cost_status"] == "estimated" and got["totals"]["unpriced_calls"] == 0
    assert got["totals"]["cost_micros"] == 120


async def test_usage_totals_say_unpriced_when_calls_used_tokens_but_cost_nothing(room):
    await ledger_row(room, cost=0)
    await ledger_row(room, cost=0)
    got = await usage(room)
    assert got["cost_status"] == "unpriced" and got["totals"]["unpriced_calls"] == 2


async def test_usage_totals_say_partial_for_a_mix(room):
    await ledger_row(room, cost=50)
    await ledger_row(room, cost=0)
    got = await usage(room)
    assert got["cost_status"] == "partial" and (
        got["totals"]["unpriced_calls"],
        got["totals"]["token_calls"],
    ) == (1, 2)
