"""AI settings validation: nothing is hardcoded, anything missing leaves AI off or fails loudly."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.ai_helpers import ai_settings
from tests.factories import make_settings


def problems(**over) -> str:
    with pytest.raises(ValidationError) as err:
        ai_settings(**over)
    return str(err.value)


def test_ai_is_off_by_default_and_needs_no_ai_settings():
    s = make_settings()
    assert s.ai_enabled is False and s.ai_llm_model == "" and not s.ai_prices_configured


def test_a_complete_configuration_is_accepted():
    s = ai_settings()
    assert s.ai_enabled and s.ai_prices_configured and s.ai_key_policy == "user_if_present"


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"ai_llm_model": ""}, "AI_LLM_MODEL"),
        ({"ai_embed_model": " "}, "AI_EMBED_MODEL"),
        ({"ai_llm_context_tokens": 0}, "AI_LLM_CONTEXT_TOKENS"),
        ({"ai_embed_dimensions": 0}, "AI_EMBED_DIMENSIONS"),
        ({"ai_embed_max_input_tokens": 0}, "AI_EMBED_MAX_INPUT_TOKENS"),
        ({"ai_llm_max_output_tokens": 0}, "AI_LLM_MAX_OUTPUT_TOKENS"),
        (
            {"ai_max_input_tokens": 99_000, "ai_llm_context_tokens": 100_000},
            "must fit in AI_LLM_CONTEXT",
        ),
        ({"ai_max_output_tokens": 5_000}, "must not exceed AI_LLM_MAX_OUTPUT_TOKENS"),
        ({"ai_key_policy": "server_only", "ai_google_api_key": ""}, "server_only needs both"),
        ({"ai_key_encryption_keys": ""}, "AI_KEY_ENCRYPTION_KEYS must be set"),
        ({"ai_key_encryption_keys": "k1:bm90MzJieXRlcw=="}, "32 bytes"),
        ({"ai_prices_checked_on": ""}, "AI_PRICES_CHECKED_ON"),
        ({"ai_prices_checked_on": "yesterday"}, "AI_PRICES_CHECKED_ON"),
        ({"ai_llm_input_micros_per_mtok": -1}, "must be 0 or more"),
        ({"ai_openai_base_url": "http://api.example.com"}, "https"),
        ({"ai_retry_max_attempts": 0}, "AI_RETRY_MAX_ATTEMPTS"),
        ({"ai_token_safety_margin": 0.5}, "AI_TOKEN_SAFETY_MARGIN"),
    ],
)
def test_bad_ai_settings_are_named(over, needle):
    assert needle in problems(**over)


def test_budgets_cannot_be_set_without_prices():
    text = problems(
        ai_budget_user_daily_micros=1000,
        ai_llm_input_micros_per_mtok=None,
        ai_llm_output_micros_per_mtok=None,
        ai_embed_text_micros_per_mtok=None,
        ai_embed_image_micros_per_mtok=None,
        ai_prices_checked_on="",
    )
    assert "budgets need the prices" in text


def test_without_prices_only_token_caps_apply():
    s = ai_settings(
        ai_llm_input_micros_per_mtok=None,
        ai_llm_output_micros_per_mtok=None,
        ai_embed_text_micros_per_mtok=None,
        ai_embed_image_micros_per_mtok=None,
        ai_prices_checked_on="",
    )
    assert not s.ai_prices_configured and s.ai_max_input_tokens == 50_000


def test_no_secret_value_appears_in_a_validation_message():
    text = problems(
        ai_key_encryption_keys="k1:not-base64!!", ai_openai_api_key="sk-super-secret-value-1234"
    )
    assert "sk-super-secret-value-1234" not in text and "not-base64" not in text


def test_user_only_policy_does_not_need_server_keys():
    s = ai_settings(ai_key_policy="user_only", ai_openai_api_key="", ai_google_api_key="")
    assert s.ai_key_policy == "user_only"
