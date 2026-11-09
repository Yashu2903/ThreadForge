"""AI checkpoint 1, part A: encryption, provider adapters, the call policy, the ledger and budgets.

Adapters run for real against HTTP-level stubs (tests/ai_helpers.Stub); the ledger and budgets run
against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx2
import pytest

from app.ai import errors as perr
from app.ai.calls import CapExceeded, KeyLimiter, MeteredCaller, RequestMeter
from app.ai.crypto import DecryptionError, KeySet, generate_key_set_entry, key_aad
from app.ai.pricing import Prices, estimate_text_tokens
from app.ai.providers.google_embed import GoogleEmbedding
from app.ai.providers.openai_llm import OpenAILLM
from app.ai.selfcheck import solid_png
from app.ai.spend import BudgetExhausted, CallContext, Spend
from app.ai.types import ImagePart, TextPart, Usage
from tests.ai_helpers import (
    SERVER_GOOGLE_KEY,
    SERVER_OPENAI_KEY,
    USER_OPENAI_KEY,
    Stub,
    ai_settings,
)
from tests.helpers import rows

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


# ---- encryption --------------------------------------------------------------------------------


def test_aes_gcm_round_trip_and_aad_binding():
    keyset = KeySet.parse(generate_key_set_entry("k1"))
    alice, bob = uuid.uuid4(), uuid.uuid4()
    key_id, blob = keyset.encrypt(b"sk-secret-value", key_aad(alice, "openai"))
    assert b"sk-secret-value" not in blob
    assert keyset.decrypt(key_id, blob, key_aad(alice, "openai")) == b"sk-secret-value"
    # the same ciphertext cannot be moved to another user or another provider
    with pytest.raises(DecryptionError):
        keyset.decrypt(key_id, blob, key_aad(bob, "openai"))
    with pytest.raises(DecryptionError):
        keyset.decrypt(key_id, blob, key_aad(alice, "google"))
    # a flipped bit is detected
    tampered = bytearray(blob)
    tampered[-1] ^= 1
    with pytest.raises(DecryptionError):
        keyset.decrypt(key_id, bytes(tampered), key_aad(alice, "openai"))
    with pytest.raises(DecryptionError):
        keyset.decrypt("unknown", blob, key_aad(alice, "openai"))
    # the nonce is random: the same plaintext never encrypts to the same bytes twice
    assert keyset.encrypt(b"x", b"a")[1] != keyset.encrypt(b"x", b"a")[1]


def test_key_rotation_keeps_old_keys_decryptable():
    old = KeySet.parse(generate_key_set_entry("old"))
    key_id, blob = old.encrypt(b"v", b"aad")
    old_entry = ",".join(f"{k}:{__import__('base64').b64encode(v).decode()}" for k, v in old.keys)
    rotated = KeySet.parse(generate_key_set_entry("new") + "," + old_entry)
    assert rotated.active_id == "new"
    assert rotated.decrypt(key_id, blob, b"aad") == b"v"


@pytest.mark.parametrize(
    "bad", ["", "nocolon", "k1:not-base64!!", "k1:" + "QUJD", "bad id:QUJD", "a b:x"]
)
def test_key_set_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        KeySet.parse(bad)


def test_duplicate_key_ids_are_rejected():
    entry = generate_key_set_entry("same")
    with pytest.raises(ValueError, match="twice"):
        KeySet.parse(entry + "," + entry)


# ---- error scrubbing ---------------------------------------------------------------------------


def test_scrub_removes_keys_and_truncates():
    text = (
        f"bad key {USER_OPENAI_KEY} and Bearer abcdefghijklmnop and AIzaSyA1234567890abcdef "
        + "x" * 500
    )
    cleaned = perr.scrub(text, (USER_OPENAI_KEY,))
    assert (
        USER_OPENAI_KEY not in cleaned
        and "AIza" not in cleaned
        and "abcdefghijklmnop" not in cleaned
    )
    assert len(cleaned) <= 200


# ---- OpenAI adapter ----------------------------------------------------------------------------


def openai(stub: Stub, key: str = SERVER_OPENAI_KEY, **over) -> OpenAILLM:
    return OpenAILLM(ai_settings(**over), key, stub.transports()["openai"])


async def test_openai_request_shape_and_usage():
    stub = Stub()
    llm = openai(stub)
    result = await llm.generate_structured(
        system="sys",
        parts=[TextPart("hello"), ImagePart(solid_png((220, 20, 20)), "image/png")],
        schema=SCHEMA,
        schema_name="pong",
        max_output_tokens=123,
    )
    assert result.data == {"answer": "PONG"}
    assert result.usage == Usage(120, 30, 1, False)
    sent = stub.calls("/v1/responses")[0]
    assert sent["headers"]["authorization"] == f"Bearer {SERVER_OPENAI_KEY}"
    body = sent["json"]
    assert body["model"] == "test-llm" and body["store"] is False
    assert body["max_output_tokens"] == 123 and body["instructions"] == "sys"
    fmt = body["text"]["format"]
    assert fmt == {"type": "json_schema", "name": "pong", "schema": SCHEMA, "strict": True}
    content = body["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "hello"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert "tools" not in body  # the model gets no tools
    await llm.aclose()


async def test_openai_missing_usage_is_estimated_and_flagged():
    stub = Stub()
    stub.llm_usage = None
    llm = openai(stub)
    result = await llm.generate_structured(
        system="s", parts=[TextPart("hi")], schema=SCHEMA, schema_name="pong", max_output_tokens=50
    )
    assert result.usage.estimated and result.usage.input_tokens > 0
    await llm.aclose()


async def test_openai_refusal_and_truncation_are_not_retryable():
    stub = Stub()
    stub.refuse = True
    llm = openai(stub)
    with pytest.raises(perr.ProviderError) as refused:
        await llm.generate_structured(
            system="s",
            parts=[TextPart("x")],
            schema=SCHEMA,
            schema_name="pong",
            max_output_tokens=5,
        )
    assert refused.value.code == perr.REFUSED and not refused.value.retryable
    stub.refuse = False
    stub.incomplete = True
    with pytest.raises(perr.ProviderError) as cut:
        await llm.generate_structured(
            system="s",
            parts=[TextPart("x")],
            schema=SCHEMA,
            schema_name="pong",
            max_output_tokens=5,
        )
    assert cut.value.code == perr.INCOMPLETE
    await llm.aclose()


async def test_unconstrained_mode_parses_and_repairs_once():
    stub = Stub()
    stub.llm_output_override = ["not json at all", '{"answer": "fixed"}']
    llm = openai(stub, ai_llm_structured_output=False)
    result = await llm.generate_structured(
        system="s", parts=[TextPart("x")], schema=SCHEMA, schema_name="pong", max_output_tokens=50
    )
    assert result.data == {"answer": "fixed"}
    calls = stub.calls("/v1/responses")
    assert len(calls) == 2  # exactly one repair attempt
    assert "text" not in calls[0]["json"]  # no schema-constrained decoding in this mode
    assert "JSON Schema" in calls[0]["json"]["instructions"]
    assert "not valid JSON" in calls[1]["json"]["input"][0]["content"][-1]["text"]
    assert result.usage.input_tokens == 240  # both calls are counted

    stub.llm_output_override = ["nope", "still nope"]
    with pytest.raises(perr.ProviderError) as bad:
        await llm.generate_structured(
            system="s",
            parts=[TextPart("x")],
            schema=SCHEMA,
            schema_name="pong",
            max_output_tokens=50,
        )
    assert bad.value.code == perr.BAD_OUTPUT
    assert len(stub.calls("/v1/responses")) == 4  # never a third attempt
    await llm.aclose()


async def test_rejected_key_error_never_contains_the_key():
    stub = Stub()
    stub.valid_keys.discard(USER_OPENAI_KEY)
    llm = openai(stub, key=USER_OPENAI_KEY)
    with pytest.raises(perr.ProviderError) as err:
        await llm.generate_structured(
            system="s",
            parts=[TextPart("x")],
            schema=SCHEMA,
            schema_name="pong",
            max_output_tokens=5,
        )
    assert err.value.code == perr.KEY_REJECTED and not err.value.retryable
    assert USER_OPENAI_KEY not in str(err.value) and "sk-user" not in str(err.value)
    await llm.aclose()


async def test_redirects_are_not_followed_so_a_key_cannot_leave():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(302, headers={"location": "https://evil.example/steal"})

    llm = OpenAILLM(ai_settings(), SERVER_OPENAI_KEY, httpx2.MockTransport(handler))
    with pytest.raises(perr.ProviderError):
        await llm.generate_structured(
            system="s",
            parts=[TextPart("x")],
            schema=SCHEMA,
            schema_name="pong",
            max_output_tokens=5,
        )
    assert len(seen) == 1 and seen[0].url.host == "api.openai.com"
    await llm.aclose()


# ---- Google adapter ----------------------------------------------------------------------------


def google(stub: Stub, key: str = SERVER_GOOGLE_KEY, **over) -> GoogleEmbedding:
    return GoogleEmbedding(ai_settings(**over), key, stub.transports()["google"])


async def test_google_embedding_request_shape_prefixes_and_batching():
    stub = Stub()
    emb = google(stub, ai_embed_batch_size=32)
    result = await emb.embed_texts([f"doc {i}" for i in range(70)])
    assert len(result.vectors) == 70 and all(len(v) == 8 for v in result.vectors)
    calls = stub.calls(":batchEmbedContents")
    assert [len(c["json"]["requests"]) for c in calls] == [32, 32, 6]  # batches respect the limit
    first = calls[0]
    assert first["headers"]["x-goog-api-key"] == SERVER_GOOGLE_KEY
    request = first["json"]["requests"][0]
    assert request["model"] == "models/test-embed" and request["output_dimensionality"] == 8
    assert request["content"]["parts"][0]["text"] == "title: none | text: doc 0"
    assert "task_type" not in request  # this model takes no task_type
    assert result.usage.estimated and result.usage.input_tokens > 0

    await emb.embed_query("what is red")
    q = stub.calls(":batchEmbedContents")[-1]["json"]["requests"][0]
    assert q["content"]["parts"][0]["text"] == "task: search result | query: what is red"
    await emb.aclose()


async def test_google_image_embedding_uses_inline_data_and_counts_images():
    stub = Stub()
    emb = google(stub)
    result = await emb.embed_images(
        [
            ImagePart(solid_png((220, 20, 20)), "image/png"),
            ImagePart(solid_png((20, 20, 220)), "image/png"),
        ]
    )
    parts = stub.calls(":batchEmbedContents")[0]["json"]["requests"]
    assert all("inline_data" in r["content"]["parts"][0] for r in parts)
    assert parts[0]["content"]["parts"][0]["inline_data"]["mime_type"] == "image/png"
    assert result.usage.image_count == 2 and result.usage.input_tokens == 2 * 258
    await emb.aclose()


async def test_google_dimension_mismatch_is_refused_and_normalization_is_optional():
    stub = Stub()
    emb = google(stub)
    stub.wrong_dimension = True
    with pytest.raises(perr.ProviderError) as err:
        await emb.embed_texts(["x"])
    assert err.value.code == perr.BAD_OUTPUT
    stub.wrong_dimension = False
    plain = (await emb.embed_texts(["red"])).vectors[0]
    norm = google(stub, ai_embed_normalize=True)
    unit = (await norm.embed_texts(["red"])).vectors[0]
    assert abs(sum(v * v for v in unit) - 1.0) < 1e-9 and sum(
        v * v for v in plain
    ) != pytest.approx(0)
    await emb.aclose()
    await norm.aclose()


async def test_google_invalid_key_is_reported_as_key_rejected():
    stub = Stub()
    emb = google(stub, key="AIzaNotAValidKey0123456789abc")
    with pytest.raises(perr.ProviderError) as err:
        await emb.validate_key()
    assert err.value.code == perr.KEY_REJECTED
    assert "AIzaNotAValid" not in str(err.value)
    health = await emb.health()
    assert not health.ok and health.code == perr.KEY_REJECTED
    await emb.aclose()


# ---- the call policy + ledger ------------------------------------------------------------------


async def ctx_for(alice, source="server", provider="openai"):
    return CallContext(
        user_id=alice.id,
        charged_user_id=alice.id,
        room_id=None,
        provider=provider,
        model="test-llm",
        key_source=source,
    )


class Harness:
    def __init__(self, sessionmaker, **over):
        self.settings = ai_settings(**over)
        self.spend = Spend(sessionmaker, self.settings)
        self.sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.caller = MeteredCaller(self.settings, self.spend, sleep=fake_sleep)
        self.prices = Prices.of(self.settings)
        self.stub = Stub()
        self.llm = OpenAILLM(self.settings, SERVER_OPENAI_KEY, self.stub.transports()["openai"])

    async def ask(self, ctx, **kw):
        return await self.caller.call(
            ctx,
            "answer",
            SERVER_OPENAI_KEY,
            lambda: self.llm.generate_structured(
                system="s",
                parts=[TextPart("hi")],
                schema=SCHEMA,
                schema_name="pong",
                max_output_tokens=100,
            ),
            usage_of=lambda r: r.usage,
            cost_of=self.prices.llm_cost,
            worst_case=Usage(500, 100),
            **kw,
        )


async def ledger(sessionmaker):
    return await rows(
        sessionmaker,
        "SELECT operation, status, error_code, attempt, input_tokens, output_tokens, "
        "est_cost_micros, key_source, usage_estimated FROM ai_usage_ledger ORDER BY id",
    )


async def test_successful_call_writes_one_ledger_row_with_real_usage(sessionmaker, alice):
    h = Harness(sessionmaker)
    await h.ask(await ctx_for(alice))
    [row] = await ledger(sessionmaker)
    assert row["status"] == "ok" and row["operation"] == "answer" and row["key_source"] == "server"
    assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
    assert row["est_cost_micros"] == 120 + 60  # $1/M in, $2/M out, rounded up
    assert row["usage_estimated"] is False


async def test_429_with_retry_after_is_retried_and_both_attempts_are_recorded(sessionmaker, alice):
    h = Harness(sessionmaker)
    h.stub.llm_failures = [(429, {"retry-after": "2"}, {"error": {"message": "slow down"}})]
    await h.ask(await ctx_for(alice))
    rows_ = await ledger(sessionmaker)
    assert [(r["status"], r["attempt"], r["error_code"]) for r in rows_] == [
        ("failed", 1, perr.RATE_LIMITED),
        ("ok", 2, None),
    ]
    assert h.sleeps and h.sleeps[0] >= 2.0  # Retry-After is respected


async def test_5xx_and_timeouts_retry_up_to_the_cap_then_fail_and_release(sessionmaker, alice):
    h = Harness(sessionmaker, ai_budget_user_daily_micros=10_000)
    h.stub.llm_failures = [(503, {}, {})] * 10
    with pytest.raises(perr.ProviderError) as err:
        await h.ask(await ctx_for(alice))
    assert err.value.code == perr.UNAVAILABLE
    assert len(await ledger(sessionmaker)) == h.settings.ai_retry_max_attempts == 4
    assert len(h.stub.calls("/v1/responses")) == 4
    # the reservation was given back: nothing stays set aside after a failed call
    [w] = await rows(sessionmaker, "SELECT reserved_micros, spent_micros FROM ai_budget_windows")
    assert (w["reserved_micros"], w["spent_micros"]) == (0, 0)


async def test_timeout_is_retried(sessionmaker, alice):
    h = Harness(sessionmaker)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx2.ReadTimeout("slow", request=request)
        return h.stub._openai(request)

    h.llm = OpenAILLM(h.settings, SERVER_OPENAI_KEY, httpx2.MockTransport(handler))
    await h.ask(await ctx_for(alice))
    assert [r["status"] for r in await ledger(sessionmaker)] == ["failed", "ok"]
    assert (await ledger(sessionmaker))[0]["error_code"] == perr.TIMEOUT


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, perr.BAD_REQUEST), (401, perr.KEY_REJECTED), (403, perr.KEY_REJECTED)],
)
async def test_400_401_403_are_never_retried(sessionmaker, alice, status, code):
    h = Harness(sessionmaker)
    h.stub.llm_failures = [(status, {}, {"error": {"message": "no"}})] * 3
    with pytest.raises(perr.ProviderError) as err:
        await h.ask(await ctx_for(alice))
    assert err.value.code == code
    assert len(h.stub.calls("/v1/responses")) == 1 and h.sleeps == []
    assert len(await ledger(sessionmaker)) == 1


async def test_refusal_is_failed_not_retried(sessionmaker, alice):
    h = Harness(sessionmaker)
    h.stub.refuse = True
    with pytest.raises(perr.ProviderError):
        await h.ask(await ctx_for(alice))
    assert len(h.stub.calls("/v1/responses")) == 1
    [row] = await ledger(sessionmaker)
    assert (row["status"], row["error_code"]) == ("failed", perr.REFUSED)


async def test_cancelled_request_makes_no_call_and_is_recorded(sessionmaker, alice):
    h = Harness(sessionmaker)

    async def cancelled() -> bool:
        return True

    with pytest.raises(perr.ProviderError) as err:
        await h.ask(await ctx_for(alice), is_cancelled=cancelled)
    assert err.value.code == perr.CANCELLED and h.stub.requests == []
    assert [r["status"] for r in await ledger(sessionmaker)] == ["cancelled"]


async def test_cancel_between_retries_stops_and_releases(sessionmaker, alice):
    h = Harness(sessionmaker, ai_budget_user_daily_micros=10_000)
    h.stub.llm_failures = [(503, {}, {})] * 5
    state = {"n": 0}

    async def cancelled_after_first_failure() -> bool:
        state["n"] += 1
        return state["n"] > 1  # not cancelled at the start, cancelled after the first failure

    with pytest.raises(perr.ProviderError):
        await h.ask(await ctx_for(alice), is_cancelled=cancelled_after_first_failure)
    assert [r["status"] for r in await ledger(sessionmaker)] == ["failed", "cancelled"]
    [w] = await rows(sessionmaker, "SELECT reserved_micros FROM ai_budget_windows")
    assert w["reserved_micros"] == 0


async def test_per_request_caps_are_enforced_before_sending(sessionmaker, alice):
    settings = ai_settings(ai_max_provider_calls=2, ai_max_images=1, ai_max_input_tokens=1000)
    meter = RequestMeter(settings)
    meter.before_call(100, 10, 0)
    meter.before_call(100, 10, 1)
    with pytest.raises(CapExceeded) as calls:
        meter.before_call(100, 10, 0)
    assert calls.value.cap == "max_provider_calls"
    meter2 = RequestMeter(settings)
    with pytest.raises(CapExceeded) as tokens:
        meter2.before_call(1001, 10, 0)
    assert tokens.value.cap == "max_input_tokens"
    with pytest.raises(CapExceeded) as out:
        meter2.before_call(10, settings.ai_max_output_tokens + 1, 0)
    assert out.value.cap == "max_output_tokens"
    meter3 = RequestMeter(settings)
    meter3.before_call(10, 10, 1)
    with pytest.raises(CapExceeded) as images:
        meter3.before_call(10, 10, 1)
    assert images.value.cap == "max_images"


async def test_a_cap_stops_the_call_before_any_http_request(sessionmaker, alice):
    h = Harness(sessionmaker, ai_max_provider_calls=1)
    meter = RequestMeter(h.settings)
    await h.ask(await ctx_for(alice), meter=meter)
    with pytest.raises(CapExceeded):
        await h.ask(await ctx_for(alice), meter=meter)
    assert len(h.stub.calls("/v1/responses")) == 1


async def test_per_key_concurrency_limit():
    limiter = KeyLimiter(2)
    running = {"now": 0, "max": 0}

    async def work():
        async with limiter.slot("openai", "sk-key-A"):
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            await asyncio.sleep(0.02)
            running["now"] -= 1

    await asyncio.gather(*(work() for _ in range(8)))
    assert running["max"] == 2
    # a different key has its own slots
    assert limiter.slot("openai", "sk-key-A") is not limiter.slot("openai", "sk-key-B")
    assert limiter.slot("openai", "sk-key-A") is limiter.slot("openai", "sk-key-A")


# ---- budgets -----------------------------------------------------------------------------------


async def test_concurrent_reservations_never_overshoot_the_budget(sessionmaker, alice):
    settings = ai_settings(ai_budget_user_daily_micros=100)
    spend = Spend(sessionmaker, settings)
    ctx = await ctx_for(alice)
    results = await asyncio.gather(
        *(spend.reserve(ctx, 30) for _ in range(12)), return_exceptions=True
    )
    granted = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, BudgetExhausted)]
    assert len(granted) == 3 and len(refused) == 9  # 3 x 30 = 90 fits; a 4th would be 120 > 100
    [w] = await rows(sessionmaker, "SELECT reserved_micros, spent_micros FROM ai_budget_windows")
    assert w["reserved_micros"] == 90 and w["spent_micros"] == 0


async def test_a_refusal_in_one_window_rolls_back_the_others(sessionmaker, alice):
    settings = ai_settings(
        ai_budget_global_monthly_micros=10_000,
        ai_budget_user_daily_micros=10_000,
        ai_budget_room_daily_micros=50,
    )
    spend = Spend(sessionmaker, settings)
    ctx = CallContext(alice.id, alice.id, uuid.uuid4(), "openai", "m", "server")
    await spend.reserve(ctx, 30)
    with pytest.raises(BudgetExhausted) as err:
        await spend.reserve(ctx, 30)  # the room window (limit 50) refuses the second
    assert err.value.scope == "room"
    got = {
        r["scope"]: r["reserved_micros"]
        for r in await rows(sessionmaker, "SELECT scope, reserved_micros FROM ai_budget_windows")
    }
    assert got == {"global": 30, "user": 30, "room": 30}  # the failed attempt left nothing behind


async def test_settle_replaces_the_reservation_and_release_gives_it_back(sessionmaker, alice):
    settings = ai_settings(ai_budget_user_daily_micros=1000)
    spend = Spend(sessionmaker, settings)
    ctx = await ctx_for(alice)
    r1 = await spend.reserve(ctx, 400)
    await spend.settle(r1, 150)  # the call cost less than the worst case
    r2 = await spend.reserve(ctx, 300)
    await spend.release(r2)
    [w] = await rows(sessionmaker, "SELECT reserved_micros, spent_micros FROM ai_budget_windows")
    assert (w["reserved_micros"], w["spent_micros"]) == (0, 150)


async def test_at_100_percent_new_server_key_calls_are_refused(sessionmaker, alice):
    settings = ai_settings(ai_budget_user_daily_micros=100)
    spend = Spend(sessionmaker, settings)
    ctx = await ctx_for(alice)
    await spend.settle(await spend.reserve(ctx, 100), 100)
    with pytest.raises(BudgetExhausted):
        await spend.reserve(ctx, 1)


async def test_user_key_calls_are_exempt_from_server_budgets(sessionmaker, alice):
    settings = ai_settings(ai_budget_user_daily_micros=1, ai_budget_global_monthly_micros=1)
    spend = Spend(sessionmaker, settings)
    assert await spend.reserve(await ctx_for(alice, source="user"), 10_000) is None
    assert await rows(sessionmaker, "SELECT * FROM ai_budget_windows") == []


async def test_warnings_fire_once_per_threshold(sessionmaker, alice):
    settings = ai_settings(ai_budget_user_daily_micros=1000)
    spend = Spend(sessionmaker, settings)
    ctx = await ctx_for(alice)
    for amount in (400, 150, 100, 400):  # cumulative 400, 550 (50%), 650, 1050 (80% and 100%)
        await spend.settle(await spend.reserve(ctx, min(amount, 100)), amount)
    seen = await rows(
        sessionmaker, "SELECT scope, threshold FROM ai_budget_warnings ORDER BY threshold"
    )
    assert [(w["scope"], w["threshold"]) for w in seen] == [
        ("user", 50),
        ("user", 80),
        ("user", 100),
    ]
    with pytest.raises(BudgetExhausted):  # over 100 percent: refused, and no new warning appears
        await spend.reserve(ctx, 1)
    assert len(await rows(sessionmaker, "SELECT * FROM ai_budget_warnings")) == 3


async def test_cost_arithmetic_rounds_up_and_unset_prices_cost_nothing():
    prices = Prices.of(
        ai_settings(ai_llm_input_micros_per_mtok=100_000, ai_llm_output_micros_per_mtok=500_000)
    )
    assert prices.llm_cost(Usage(1, 0)) == 1  # a tiny call is never free
    assert prices.llm_cost(Usage(1_000_000, 1_000_000)) == 100_000 + 500_000
    unset = Prices.of(
        ai_settings(
            ai_llm_input_micros_per_mtok=None,
            ai_llm_output_micros_per_mtok=None,
            ai_embed_text_micros_per_mtok=None,
            ai_embed_image_micros_per_mtok=None,
            ai_prices_checked_on="",
        )
    )
    assert unset.llm_cost(Usage(10**6, 10**6)) == 0 and not unset.llm_known
    # image embedding falls back to the text rate only when its own price is unset
    fallback = Prices.of(ai_settings(ai_embed_image_micros_per_mtok=None))
    assert fallback.embed_image_cost(Usage(1_000_000, 0)) == 200_000


def test_token_estimate_is_conservative():
    settings = ai_settings()
    text = "word " * 1000  # 5000 chars: ~1250 real tokens
    assert estimate_text_tokens(settings, text) >= 1250
    assert estimate_text_tokens(settings, "") == 0
