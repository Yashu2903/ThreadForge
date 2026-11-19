"""The `@AI` task classifier: its output contract, its failure categories, its budget, and what the request records.

Why this exists (docs/ai.md 22.7): on a reasoning model the classifier's output budget also pays for HIDDEN reasoning. With
the old fixed 50-token budget a diagram request could spend all 50 on reasoning and return nothing (a truncated reply), the
request silently fell back to `qa`, and the report still said "100% classified as expected". These tests pin the fixed
behaviour with deterministic fixtures (the real model is never faulted on purpose).

Three layers: the classification contract with a fake gateway; the real adapter and call policy against the HTTP stub; and
the real request pipeline end to end.
"""

from __future__ import annotations

import pytest

from app.ai import errors as perr
from app.ai.analysis.gateway import AnalysisAbort
from app.ai.answer import execute as ex
from app.ai.answer import prompts
from app.ai.calls import MeteredCaller
from app.ai.pricing import Prices
from app.ai.providers.openai_llm import OpenAILLM
from app.ai.spend import CallContext, Spend
from app.ai.types import TextPart, Usage
from tests.ai_helpers import SERVER_OPENAI_KEY, Stub, ai_settings
from tests.analysis_helpers import build_world
from tests.helpers import rows

# ---- layer 1: the classification contract --------------------------------------------------------------------------


class FakeGateway:
    """Stands in for AnalysisGateway: returns a value, or raises the abort a failed provider call becomes."""

    def __init__(self, result=None, abort: AnalysisAbort | None = None, **settings):
        self.settings = ai_settings(**settings)
        self.result, self.abort, self.calls = result, abort, []

    async def structured(self, operation, parts, **kwargs):
        self.calls.append({"operation": operation, "parts": parts, **kwargs})
        if self.abort:
            raise self.abort
        return self.result


async def classify(result=None, abort=None, **settings):
    gateway = FakeGateway(result, abort, **settings)
    return await ex._classify(gateway, "Draw a flowchart of the build and deploy steps."), gateway


@pytest.mark.parametrize("route", ex.ROUTES)
async def test_every_valid_route_is_a_model_classification(route):
    c, _ = await classify({"task": route})
    assert c == ex.Classification(True, route=route)
    assert c.failure is None


def test_the_routes_are_the_schemas_enum_and_nothing_else():
    assert ex.ROUTES == ("qa", "summarize", "compare", "document_review")
    assert set(ex.ROUTES) == set(prompts.CLASSIFY_SCHEMA["properties"]["task"]["enum"])


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (perr.INCOMPLETE, "truncated"),  # the reported failure
        (perr.BAD_OUTPUT, "invalid_output"),  # empty or malformed output
        (perr.REFUSED, "refused"),
        (perr.TIMEOUT, "timeout"),
        (perr.RATE_LIMITED, "provider_failure"),
        (perr.UNAVAILABLE, "provider_failure"),
        (perr.BAD_REQUEST, "provider_failure"),
    ],
)
async def test_each_failure_has_a_stable_category_and_is_not_a_classification(code, category):
    detail = {"reasoning_tokens": 50, "output_tokens": 50}
    c, _ = await classify(abort=AnalysisAbort(code, detail=detail))
    assert c.attempted and c.route is None  # never a valid route
    assert (c.failure, c.failure_code) == (category, code)
    assert c.detail == detail  # safe numbers travel with it


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ({"oops": 1}, "missing_route"),
        ({}, "missing_route"),
        (["qa"], "missing_route"),
        (None, "missing_route"),
        ({"task": None}, "missing_route"),
        ({"task": "poem"}, "unsupported_route"),
        ({"task": "QA"}, "unsupported_route"),  # the enum is exact
        ({"task": ""}, "unsupported_route"),
        ({"task": " qa"}, "unsupported_route"),
    ],
)
async def test_a_reply_that_is_not_one_of_the_routes_is_invalid_output(reply, code):
    c, _ = await classify(reply)
    assert (c.attempted, c.route, c.failure, c.failure_code) == (True, None, "invalid_output", code)


@pytest.mark.parametrize(
    "code", ["budget_exhausted", "key_rejected", "key_unavailable", "analysis_cap_exceeded"]
)
async def test_a_budget_or_key_problem_still_stops_the_request(code):
    with pytest.raises(AnalysisAbort):
        await classify(abort=AnalysisAbort(code))


async def test_the_classifier_settings_reach_the_gateway_call():
    _, gateway = await classify({"task": "qa"})  # defaults: bounded budget, effort not sent
    call = gateway.calls[0]
    assert call["max_output_tokens"] == 512 == ai_settings().ai_classifier_max_output_tokens
    assert call["reasoning_effort"] is None
    assert call["schema"] == prompts.CLASSIFY_SCHEMA and call["operation"] == "classify"
    # the instruction only: no file, no document
    assert call["parts"] == [TextPart("Draw a flowchart of the build and deploy steps.")]
    _, gateway = await classify(
        {"task": "qa"}, ai_classifier_max_output_tokens=300, ai_classifier_reasoning_effort="none"
    )
    assert gateway.calls[0]["max_output_tokens"] == 300
    assert gateway.calls[0]["reasoning_effort"] == "none"


def test_the_budget_is_bounded_and_the_effort_is_validated():
    # validated when the settings are built: a bad value cannot start the application
    for bad, name in (
        ({"ai_classifier_max_output_tokens": 8}, "AI_CLASSIFIER_MAX_OUTPUT_TOKENS"),
        ({"ai_classifier_max_output_tokens": 10_000}, "AI_CLASSIFIER_MAX_OUTPUT_TOKENS"),
        ({"ai_classifier_reasoning_effort": "bananas"}, "AI_CLASSIFIER_REASONING_EFFORT"),
    ):
        with pytest.raises(ValueError, match=name):
            ai_settings(**bad)
    ok = ai_settings(ai_classifier_reasoning_effort="low", ai_classifier_max_output_tokens=16)
    assert (ok.ai_classifier_reasoning_effort, ok.ai_classifier_max_output_tokens) == ("low", 16)


# ---- the routing record ----------------------------------------------------------------------------------------------


def test_a_fallback_that_lands_on_the_expected_route_is_still_a_failure_and_a_fallback():
    failed = ex.Classification(True, failure="truncated", failure_code=perr.INCOMPLETE)
    final = ex.resolve_task("auto", failed.route, n_sources=1, has_doc=False)
    record = ex.routing_record("auto", failed, final)
    assert final == "qa"  # the expected route, reached by the fallback
    assert record["model_route"] is None  # NOT a model classification
    assert record["failure"] == "truncated" and record["fallback_used"] is True
    assert record["fallback_reason"] == "truncated" and record["degraded"] is True
    assert record["final_route"] == "qa" and record["classification_attempted"] is True


def test_a_fallback_can_land_on_a_different_route_than_the_model_would_have_chosen():
    # the model would have said "summarize"; the failure sends the request down qa instead
    failed = ex.Classification(True, failure="invalid_output", failure_code="unsupported_route")
    record = ex.routing_record(
        "auto", failed, ex.resolve_task("auto", None, n_sources=2, has_doc=False)
    )
    assert (record["model_route"], record["final_route"], record["fallback_used"]) == (
        None,
        "qa",
        True,
    )


def test_a_valid_classification_is_not_degraded_even_when_narrowed_to_the_context():
    # a valid "compare" with one file runs as qa: narrowed, not a failure and not a fallback
    ok = ex.Classification(True, route="compare")
    record = ex.routing_record(
        "auto", ok, ex.resolve_task("auto", "compare", n_sources=1, has_doc=False)
    )
    assert record["model_route"] == "compare" and record["final_route"] == "qa"
    assert record["route_adjusted_to_context"] is True
    assert (
        record["fallback_used"] is False
        and record["degraded"] is False
        and record["failure"] is None
    )


def test_without_auto_classification_nothing_is_attempted_and_nothing_is_a_fallback():
    record = ex.routing_record("ask", ex.Classification(attempted=False), "qa")
    assert record["classification_attempted"] is False and record["fallback_used"] is False
    assert record["degraded"] is False and record["model_route"] is None


# ---- layer 2: the real adapter and call policy against the HTTP stub -----------------------------------------------------------

TASK_SCHEMA = prompts.CLASSIFY_SCHEMA
TRUNCATED_ON_REASONING = {
    "input_tokens": 138,
    "output_tokens": 50,
    "output_tokens_details": {"reasoning_tokens": 50},
}


def adapter(stub: Stub, **settings) -> OpenAILLM:
    return OpenAILLM(ai_settings(**settings), SERVER_OPENAI_KEY, stub.transports()["openai"])


async def structured(llm: OpenAILLM, **kw):
    return await llm.generate_structured(
        system=prompts.CLASSIFY_SYSTEM,
        parts=[TextPart("Draw a flowchart")],
        schema=TASK_SCHEMA,
        schema_name=prompts.CLASSIFY_SCHEMA_NAME,
        max_output_tokens=kw.pop("max_output_tokens", 512),
        **kw,
    )


async def test_the_budget_and_effort_reach_the_provider_request():
    stub = Stub()
    stub.llm_output_override = '{"task": "qa"}'
    llm = adapter(stub)
    await structured(llm, max_output_tokens=512)
    body = stub.calls("/v1/responses")[0]["json"]
    assert body["max_output_tokens"] == 512
    assert "reasoning" not in body  # unset: the parameter is not sent
    await structured(llm, max_output_tokens=300, reasoning_effort="none")
    body = stub.calls("/v1/responses")[1]["json"]
    assert body["max_output_tokens"] == 300 and body["reasoning"] == {"effort": "none"}
    assert body["text"]["format"]["strict"] is True  # still the strict enum schema


async def test_a_per_call_effort_overrides_the_global_one_and_none_leaves_it_alone():
    stub = Stub()
    stub.llm_output_override = '{"task": "qa"}'
    llm = adapter(stub, ai_llm_reasoning_effort="high")
    await structured(llm)  # no per-call value: the global one
    await structured(llm, reasoning_effort="low")  # the per-call one wins
    bodies = [c["json"] for c in stub.calls("/v1/responses")]
    assert [b["reasoning"] for b in bodies] == [{"effort": "high"}, {"effort": "low"}]


async def test_a_truncated_reply_keeps_what_the_provider_reported_it_spent():
    stub = Stub()
    stub.incomplete_names = {"task_kind"}
    stub.incomplete_usage = TRUNCATED_ON_REASONING
    stub.incomplete_output = [{"type": "reasoning"}]  # reasoning only: no visible text at all
    with pytest.raises(perr.ProviderError) as err:
        await structured(adapter(stub), max_output_tokens=50)
    e = err.value
    assert e.code == perr.INCOMPLETE and not e.retryable
    assert e.usage == Usage(138, 50, 0)  # billed, so recorded
    assert e.detail == {
        "incomplete_reason": "max_output_tokens",
        "output_tokens": 50,
        "reasoning_tokens": 50,
        "had_visible_output": False,
    }
    assert (
        "reasoning" not in str(e).lower() or str(e) == "the output was cut off"
    )  # no reply text in the message


async def test_a_truncated_reply_that_reports_no_usage_stays_unknown_not_zero():
    stub = Stub()
    stub.incomplete_names = {"task_kind"}  # no usage reported
    with pytest.raises(perr.ProviderError) as err:
        await structured(adapter(stub))
    assert err.value.usage is None  # unknown: never invented


async def test_empty_and_malformed_output_are_bad_output():
    for script in ('{"task": ', "not json at all", ""):
        stub = Stub()
        stub.llm_output_override = script
        with pytest.raises(perr.ProviderError) as err:
            await structured(adapter(stub))
        assert err.value.code == perr.BAD_OUTPUT


async def test_a_refusal_is_a_refusal():
    stub = Stub()
    stub.refuse = True
    with pytest.raises(perr.ProviderError) as err:
        await structured(adapter(stub))
    assert err.value.code == perr.REFUSED


class Policy:
    """The real call policy over the real ledger, with a recorded (not slept) back-off."""

    def __init__(self, sessionmaker, **settings):
        self.settings = ai_settings(**settings)
        self.stub = Stub()
        self.sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.caller = MeteredCaller(
            self.settings, Spend(sessionmaker, self.settings), sleep=fake_sleep
        )
        self.llm = OpenAILLM(self.settings, SERVER_OPENAI_KEY, self.stub.transports()["openai"])

    async def classify(self, alice):
        ctx = CallContext(alice.id, alice.id, None, "openai", "test-llm", "server")
        return await self.caller.call(
            ctx,
            "classify",
            SERVER_OPENAI_KEY,
            lambda: structured(self.llm, max_output_tokens=50),
            usage_of=lambda r: r.usage,
            cost_of=Prices.of(self.settings).llm_cost,
            worst_case=Usage(200, 50),
        )


async def ledger(sessionmaker):
    return await rows(
        sessionmaker,
        "SELECT operation, status, error_code, attempt, input_tokens, output_tokens, est_cost_micros "
        "FROM ai_usage_ledger ORDER BY id",
    )


async def test_a_truncated_attempt_is_recorded_with_its_real_tokens_and_is_not_retried(
    sessionmaker, alice
):
    p = Policy(sessionmaker)
    p.stub.incomplete_names = {"task_kind"}
    p.stub.incomplete_usage = TRUNCATED_ON_REASONING
    with pytest.raises(perr.ProviderError):
        await p.classify(alice)
    assert len(p.stub.calls("/v1/responses")) == 1 and p.sleeps == []  # no retry, no back-off
    [row] = await ledger(sessionmaker)
    assert (row["status"], row["error_code"], row["attempt"]) == ("failed", perr.INCOMPLETE, 1)
    assert (row["input_tokens"], row["output_tokens"]) == (138, 50)  # was 0 / 0 before the fix
    assert row["est_cost_micros"] == 138 + 100  # priced: $1/M in, $2/M out, rounded up


async def test_retries_stay_bounded_and_every_attempt_is_accounted(sessionmaker, alice):
    p = Policy(sessionmaker, ai_retry_max_attempts=3)
    p.stub.llm_failures = [(503, {}, {})] * 10
    with pytest.raises(perr.ProviderError) as err:
        await p.classify(alice)
    assert err.value.code == perr.UNAVAILABLE
    assert len(p.stub.calls("/v1/responses")) == 3  # exactly the configured cap, not unlimited
    assert len(p.sleeps) == 2
    got = await ledger(sessionmaker)
    assert [(r["status"], r["attempt"], r["error_code"]) for r in got] == [
        ("failed", 1, perr.UNAVAILABLE),
        ("failed", 2, perr.UNAVAILABLE),
        ("failed", 3, perr.UNAVAILABLE),
    ]
    assert all(r["input_tokens"] == 0 for r in got)  # a 503 spent nothing: zero is the truth here


async def test_a_failed_attempt_followed_by_a_success_records_both_without_double_counting(
    sessionmaker, alice
):
    p = Policy(sessionmaker)
    p.stub.llm_failures = [(503, {}, {})]
    p.stub.llm_output_override = '{"task": "qa"}'
    await p.classify(alice)
    got = await ledger(sessionmaker)
    assert [(r["status"], r["attempt"]) for r in got] == [("failed", 1), ("ok", 2)]
    assert (
        sum(r["input_tokens"] for r in got) == 120
    )  # the success's usage once; the 503 added nothing


# ---- layer 3: the real request pipeline ----------------------------------------------------------------------------------------

OPS = (
    b"# Operations\n\nThe warehouse processed twelve thousand orders in March. Delivery drivers were "
    b"short, so deliveries slipped.\n"
)


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def run_request(world, instruction="Draw a flowchart of the process", **kw):
    source = await world.upload(world.owner, "ops.md", OPS)
    assert (await world.analyze(world.owner, source)).status_code == 202
    assert set(await world.run_worker()) == {"succeeded"}
    r = await world.send(world.owner, instruction, sources=[source], **kw)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    await world.drain()
    result = await world.request(world.owner, rid, "/result")
    assert result.status_code == 200, result.text
    return rid, result.json()


async def test_a_truncated_classification_still_publishes_and_is_recorded_as_degraded(world):
    world.stub.incomplete_names = {"task_kind"}
    world.stub.incomplete_usage = TRUNCATED_ON_REASONING
    world.stub.incomplete_output = [{"type": "reasoning"}]
    rid, result = await run_request(world, task_hint="auto")
    routing = result["coverage"]["routing"]
    assert routing == {
        "hint": "auto",
        "classification_attempted": True,
        "model_route": None,  # not a model classification
        "failure": "truncated",
        "failure_code": perr.INCOMPLETE,
        "fallback_used": True,
        "fallback_reason": "truncated",
        "final_route": "qa",
        "route_adjusted_to_context": False,
        "degraded": True,
    }
    assert result["task_type"] == "qa"
    # the failed attempt is in the ledger with its real tokens and its stable code
    [bad] = await rows(
        world.sm,
        "SELECT status, error_code, input_tokens, output_tokens FROM ai_usage_ledger "
        "WHERE request_id = :r AND operation = 'classify'",
        r=rid,
    )
    assert (bad["status"], bad["error_code"]) == ("failed", perr.INCOMPLETE)
    assert (bad["input_tokens"], bad["output_tokens"]) == (138, 50)
    # the request's own usage includes the billed failure once, and says so
    usage = (await rows(world.sm, "SELECT usage FROM ai_results WHERE request_id = :r", r=rid))[0][
        "usage"
    ]
    assert usage["llm_failed_calls"] == 1
    manifest = (await rows(world.sm, "SELECT data FROM ai_manifests WHERE request_id = :r", r=rid))[
        0
    ]["data"]
    assert manifest["routing"]["detail"] == {
        "incomplete_reason": "max_output_tokens",
        "output_tokens": 50,
        "reasoning_tokens": 50,
        "had_visible_output": False,
    }
    assert "detail" not in result["coverage"]["routing"]  # numbers stay out of the ordinary result


async def test_a_valid_classification_is_recorded_as_one(world):
    world.stub.classify_as = "summarize"
    _, result = await run_request(world, "Give me the gist", task_hint="auto")
    routing = result["coverage"]["routing"]
    assert routing["model_route"] == "summarize" and routing["final_route"] == "summarize"
    assert routing["classification_attempted"] is True and routing["failure"] is None
    assert routing["fallback_used"] is False and routing["degraded"] is False


async def test_an_unsupported_reply_is_invalid_output_and_falls_back(world):
    world.stub.classify_as = "poem"  # valid JSON, not a route
    _, result = await run_request(world, task_hint="auto")
    routing = result["coverage"]["routing"]
    assert (routing["failure"], routing["failure_code"]) == ("invalid_output", "unsupported_route")
    assert (routing["model_route"], routing["final_route"], routing["fallback_used"]) == (
        None,
        "qa",
        True,
    )


async def test_a_chosen_hint_attempts_no_classification(world):
    _, result = await run_request(world, task_hint="ask")
    routing = result["coverage"]["routing"]
    assert routing["classification_attempted"] is False and routing["fallback_used"] is False
    assert not [c for c in world.stub.calls("/v1/responses") if "task_kind" in str(c["json"])]


async def test_the_classifier_call_that_reaches_the_provider_carries_the_configured_budget(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor,
        sessionmaker,
        tmp_path,
        redis_prefix,
        ai_classifier_max_output_tokens=222,
        ai_classifier_reasoning_effort="none",
    )
    await run_request(world, task_hint="auto")
    [call] = [
        c
        for c in world.stub.calls("/v1/responses")
        if c["json"]["text"]["format"]["name"] == "task_kind"
    ]
    assert call["json"]["max_output_tokens"] == 222
    assert call["json"]["reasoning"] == {"effort": "none"}
    # and the answer call did not inherit the classifier's settings
    [answer] = [
        c
        for c in world.stub.calls("/v1/responses")
        if c["json"]["text"]["format"]["name"] == "cited_answer"
    ]
    assert answer["json"]["max_output_tokens"] != 222 and "reasoning" not in answer["json"]
