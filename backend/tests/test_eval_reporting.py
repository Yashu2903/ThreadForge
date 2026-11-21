"""The evaluation reporting (tests/eval/): routing metrics and their denominators, failed provider calls, cost status,
run manifests and their provenance labels, and the runner's exit status.

Deterministic fixtures only: nothing here calls a provider, and no real model response is faulted on purpose. These pin the
corrections made after the first real runs (docs/ai.md 22.7): a fallback that happened to pick the right route was reported
as "100% classified as expected"; a failed classifier call was invisible in the report; an unpriced cost printed as 0.
"""

from __future__ import annotations

import copy
import json
import sys

import pytest

from tests.ai_helpers import ai_settings
from tests.eval import manifest, run_eval
from tests.eval.metrics import failed_calls, fraction, routing_metrics
from tests.eval.test_eval import build_report, cost_lines, failure_lines, ratio
from tests.eval.test_proposal_eval import GATE
from tests.eval.test_proposal_eval import build_report as proposal_report
from tests.helpers import execute

# ---- routing metrics -------------------------------------------------------------------------------------------------


def routing(model_route=None, final="qa", failure=None, attempted=True):
    fallback = attempted and model_route is None
    return {
        "hint": "auto" if attempted else "ask",
        "classification_attempted": attempted,
        "model_route": model_route,
        "failure": failure,
        "failure_code": None,
        "fallback_used": fallback,
        "fallback_reason": failure if fallback else None,
        "final_route": final,
        "route_adjusted_to_context": False,
        "degraded": fallback,
    }


def rec(id, expected, route=None, published=True):
    return {"id": id, "expected_route": expected, "published": published, "routing": route}


MIXED = [
    rec("model-right", "qa", routing("qa", "qa")),
    rec("model-wrong", "qa", routing("summarize", "summarize")),
    rec("fallback-right", "qa", routing(None, "qa", "truncated")),
    rec("fallback-wrong", "summarize", routing(None, "qa", "truncated")),
    rec("no-classification", "qa", routing(None, "qa", attempted=False)),
    rec("not-published", "qa", None, published=False),
]


def test_every_ratio_carries_its_numerator_and_denominator():
    assert fraction(3, 4) == {"numerator": 3, "denominator": 4, "value": 0.75}
    assert fraction(0, 0) == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }  # an empty denominator is not 0%
    assert ratio(fraction(2, 5)) == "2/5 (40%)"
    assert ratio(fraction(0, 0)) == "0/0 (n/a)"


def test_routing_denominators_match_their_documented_definitions():
    m = routing_metrics(MIXED)
    assert m["published"] == fraction(5, 6)  # all requests are the denominator
    assert m["classification_attempts"] == 4  # routed requests whose hint asked the model
    assert m["valid_model_classifications"] == fraction(2, 4)
    assert m["model_accuracy_of_valid"] == fraction(1, 2)  # of the valid ones
    assert m["model_accuracy_of_attempts"] == fraction(1, 4)  # a failure counts as not correct
    assert m["classifier_failures"] == fraction(2, 4)
    assert m["classifier_failures_by_category"] == {"truncated": 2}
    assert m["fallback_requests"] == fraction(2, 5)  # of the routed (published) requests
    assert m["final_route_accuracy"] == fraction(
        3, 5
    )  # model-right, fallback-right, no-classification
    assert m["final_correct_by_fallback"] == 1
    assert m["model_misrouted"] == [
        {"id": "model-wrong", "model_route": "summarize", "expected": "qa"}
    ]
    assert m["fallback_ids"] == ["fallback-right", "fallback-wrong"]


def test_a_fallback_that_lands_on_the_expected_route_is_not_a_model_classification_success():
    m = routing_metrics([rec("only", "qa", routing(None, "qa", "truncated"))])
    assert m["valid_model_classifications"] == fraction(0, 1)
    assert m["model_accuracy_of_valid"] == fraction(0, 0)  # nothing was classified by the model
    assert m["model_accuracy_of_attempts"] == fraction(0, 1)
    assert m["classifier_failures"] == fraction(1, 1)
    assert m["fallback_requests"] == fraction(1, 1)
    # ...while the final route is right: all three facts coexist
    assert m["final_route_accuracy"] == fraction(1, 1) and m["final_correct_by_fallback"] == 1


def test_final_routing_success_coexists_with_classifier_failure():
    m = routing_metrics([rec(f"r{i}", "qa", routing(None, "qa", "truncated")) for i in range(3)])
    assert m["final_route_accuracy"]["value"] == 1.0
    assert m["classifier_failures"]["value"] == 1.0
    assert m["valid_model_classifications"]["value"] == 0.0


def test_failure_categories_are_counted_separately():
    records = [
        rec("a", "qa", routing(None, "qa", "truncated")),
        rec("b", "qa", routing(None, "qa", "invalid_output")),
        rec("c", "qa", routing(None, "qa", "timeout")),
        rec("d", "qa", routing(None, "qa", "truncated")),
    ]
    assert routing_metrics(records)["classifier_failures_by_category"] == {
        "truncated": 2,
        "invalid_output": 1,
        "timeout": 1,
    }


# ---- provider failures stay visible ---------------------------------------------------------------------------------------

LEDGER = [
    {"operation": "answer", "provider": "openai", "model": "m", "status": "ok", "error_code": "-", "calls": 13, "input_tokens": 100, "output_tokens": 10},
    {"operation": "classify", "provider": "openai", "model": "m", "status": "ok", "error_code": "-", "calls": 11, "input_tokens": 40, "output_tokens": 5},
    {"operation": "classify", "provider": "openai", "model": "m", "status": "failed", "error_code": "provider_output_truncated", "calls": 2, "input_tokens": 276, "output_tokens": 100},
    {"operation": "embed_text", "provider": "google", "model": "e", "status": "ok", "error_code": "-", "calls": 16, "input_tokens": 4000, "output_tokens": 0},
]  # fmt: skip


def test_failed_calls_are_counted_by_operation_and_code():
    f = failed_calls(LEDGER)
    assert f["calls"] == fraction(2, 42)
    assert f["by_operation_and_code"] == [
        {"operation": "classify", "error_code": "provider_output_truncated", "calls": 2}
    ]
    assert failed_calls([r for r in LEDGER if r["status"] == "ok"])["by_operation_and_code"] == []


def full(id, task, expected, route):
    return {
        "id": id,
        "task": task,
        "published": True,
        "task_ok": route["final_route"] == expected,
        "expected_route": expected,
        "routing": route,
        "recall": {"@1": 1.0, "@3": 1.0, "@5": 1.0, "@evidence": 1.0},
        "citation_clean": True,
        "cites_expected_sources": True,
        "cited_text_has_needles": True,
        "no_repair": True,
        "diagram_published": None,
        "diagram_renders": None,
        "figure_cited": None,
        "coverage_full": None,
        "latency_s": 1.0,
        "ledger": {"calls": 3, "answer_calls": 1, "input_tokens": 100, "output_tokens": 10, "cost_micros": 0, "failed_calls": 0},
    }  # fmt: skip


def test_the_report_shows_the_failed_call_and_the_fallback_even_though_every_request_published():
    records = [
        full("ok", "qa", "qa", routing("qa", "qa")),
        full("diagram", "diagram", "qa", routing(None, "qa", "truncated")),
    ]
    run = {"failures": failed_calls(LEDGER), "cost": manifest.cost_summary(ai_settings(), LEDGER)}
    report = build_report("real", 0.92, {}, records, run)
    assert "Requests that published an answer | 100% (2/2)" in report
    assert "provider_output_truncated" in report and "Failed provider calls: 2/42" in report
    assert "Requests that ran the fallback | 1/2" in report
    assert "'truncated': 1" in report
    # the row that used to mask the failure is gone; its replacement says what it counts
    assert "Task type classified as expected" not in report
    assert "Final route matched the expected route (fallback included)" in report
    assert "never counts as a model classification" in report


def test_the_report_states_what_the_metrics_measure_and_what_they_do_not():
    report = build_report("real", 0.92, {}, [full("ok", "qa", "qa", routing("qa", "qa"))], None)
    for phrase in (
        "RETRIEVED PASSAGES",  # the recall unit
        "required phrases",  # how expected evidence is defined
        "does NOT require an answer to cite anything",  # what citation validity is
        "Denominators",
        "Not assessed here",  # factual support and usefulness
        "no pass/fail threshold",
    ):
        assert phrase in report, phrase


def test_the_proposal_gate_is_unchanged_and_the_report_still_reports_failures():
    assert GATE == 0.90

    def p(i, published):
        return {"id": f"p{i}", "kind": "edit", "published": published, "first_try_valid": published, "repaired": False,
                "outcome": "published" if published else "invalid", "problems": [], "resolves": True,
                "citation_valid": None, "latency_s": 1.0, "ledger": {"answer_calls": 1}, "preview": {}, "instruction": "x"}  # fmt: skip

    run = {"failures": failed_calls(LEDGER), "cost": manifest.cost_summary(ai_settings(), LEDGER)}
    report, final = proposal_report("real", [p(i, True) for i in range(16)], run)
    assert final == 1.0 and "PASS" in report and "provider_output_truncated" in report
    report, final = proposal_report("real", [p(i, i < 14) for i in range(16)], run)
    assert final == 14 / 16 < GATE and "FAIL" in report


# ---- cost status -----------------------------------------------------------------------------------------------------------

UNPRICED = dict(
    ai_llm_input_micros_per_mtok=None,
    ai_llm_output_micros_per_mtok=None,
    ai_embed_text_micros_per_mtok=None,
    ai_embed_image_micros_per_mtok=None,
    ai_prices_checked_on="",
)


def test_missing_prices_give_unknown_cost_never_zero_and_the_tokens_are_kept():
    c = manifest.cost_summary(ai_settings(**UNPRICED), LEDGER)
    assert c["status"] == "unpriced" and c["est_cost_micros"] is None and c["est_cost_usd"] is None
    assert c["complete_total"] is False and c["rates"] is None
    assert set(c["unpriced_components"]) == {"generation", "embedding"}
    assert "not priced" in c["kind_of_value"]
    assert c[
        "usage_retained"
    ] == {  # generation and embedding stay separate; failed calls' tokens are included
        "generation_input_tokens": 100 + 40 + 276,
        "generation_output_tokens": 10 + 5 + 100,
        "embedding_input_tokens": 4000,
    }


def test_known_zero_usage_is_distinct_from_missing_pricing():
    zero = manifest.cost_summary(ai_settings(**UNPRICED), [])
    assert (
        zero["status"] == "none" and zero["est_cost_micros"] == 0 and zero["complete_total"] is True
    )
    priced_zero = manifest.cost_summary(ai_settings(), [])
    assert priced_zero["status"] == "none" and priced_zero["est_cost_micros"] == 0
    unknown = manifest.cost_summary(ai_settings(**UNPRICED), LEDGER)
    assert unknown["est_cost_micros"] is None  # tokens were used: unknown, not zero


def test_an_estimated_cost_names_its_currency_rates_and_provenance():
    c = manifest.cost_summary(ai_settings(), LEDGER)  # USD 1/M in, 2/M out, 0.2/M embedding
    assert c["status"] == "estimated" and c["complete_total"] is True
    assert (
        c["est_cost_micros"] == 416 + 230 + 800
    )  # generation in, generation out, embedding (rounded up)
    assert c["est_cost_usd"] == pytest.approx(0.001446)
    assert c["currency"] == "USD" and "micro-dollars" in c["unit_note"]
    assert c["rates"]["checked_on"] == "2026-11-08" and "configured rates" in c["rates"]["source"]
    assert "not provider-reported" in c["kind_of_value"]
    assert "tokens, not cost" in c["provider_reported_cost"]


def test_a_partly_priced_run_is_marked_partial_and_is_not_a_total():
    c = manifest.cost_summary(
        ai_settings(ai_embed_text_micros_per_mtok=None, ai_embed_image_micros_per_mtok=None), LEDGER
    )
    assert c["status"] == "partial" and c["complete_total"] is False
    assert c["unpriced_components"] == ["embedding"]
    assert c["est_cost_micros"] == 416 + 230  # the priced part only
    assert "PARTIAL" in c["kind_of_value"]


def test_usage_is_counted_once_from_the_ledger():
    c = manifest.cost_summary(
        ai_settings(), LEDGER + []
    )  # per-request aggregates are not an input at all
    again = manifest.cost_summary(ai_settings(), copy.deepcopy(LEDGER))
    assert c == again
    assert sum(c["usage_retained"].values()) == (100 + 40 + 276) + (10 + 5 + 100) + 4000


def test_the_report_shows_unknown_cost_not_zero_when_unpriced():
    settings = ai_settings(**UNPRICED)
    run = {"failures": failed_calls(LEDGER), "cost": manifest.cost_summary(settings, LEDGER)}
    report = build_report("real", 0.92, {}, [full("ok", "qa", "qa", routing("qa", "qa"))], run)
    assert "unknown (no rates configured)" in report
    assert "0.00000" not in report
    text = "\n".join(cost_lines(run))
    assert (
        "**unpriced**" in text and "Cost: **unknown**" in text and "not priced, never free" in text
    )
    assert "None" not in "\n".join(failure_lines(run)).split("Failed provider calls")[0]


# ---- manifests and provenance ---------------------------------------------------------------------------------------------


def test_a_reconstructed_manifest_labels_every_field_and_says_it_was_built_afterwards():
    fields = {
        "code": manifest.labelled(
            "14e0cf1", "reconstructed", "a git status snapshot taken before the run"
        ),
        "started": manifest.labelled("unavailable: not recorded at the time", "unavailable"),
        "run_ended": manifest.labelled("2026-11-19T15:20:29Z", "captured_at_completion"),
    }
    m = manifest.reconstructed_manifest(
        kind="answer", mode="real", run_ended="2026-11-19T15:27:48Z", fields=fields
    )
    assert (
        m["created"]["stage"] == "after_execution"
        and "did not capture it" in m["created"]["meaning"]
    )
    assert m["evaluator_version"] == "eval-1"
    assert all(f["provenance"] in manifest.PROVENANCE for f in m["fields"].values())
    assert m["fields"]["code"]["evidence"].startswith("a git status")


def test_provenance_cannot_be_left_off_or_faked():
    with pytest.raises(ValueError, match="evidence"):
        manifest.labelled("x", "reconstructed")  # a reconstruction must say what supports it
    with pytest.raises(ValueError, match="why"):
        manifest.labelled("a value", "unavailable")  # unavailable holds a reason, not a value
    with pytest.raises(ValueError, match="unknown provenance"):
        manifest.labelled("x", "trust-me")
    with pytest.raises(ValueError, match="not labelled"):
        manifest.reconstructed_manifest(
            kind="answer", mode="real", run_ended="x", fields={"code": {"value": "14e0cf1"}}
        )


def test_secrets_never_reach_a_manifest():
    settings = ai_settings(
        ai_openai_api_key="sk-LIVE-SECRET-0123456789abcdef",
        ai_google_api_key="AIzaLIVESECRET0123456789abcdef",
    )
    started = manifest.begin(
        kind="answer", mode="real", settings=settings, dataset={"questions": 1}, expected_items=1
    )
    blob = json.dumps({"config": manifest.safe_config(settings), "begin": started}, default=str)
    for secret in ("sk-LIVE-SECRET", "AIzaLIVESECRET", "ai_key_encryption_keys"):
        assert secret not in blob
    assert set(manifest.safe_config(settings)) == set(
        manifest.CONFIG_ALLOWLIST
    )  # only allowlisted names


def test_a_manifest_is_captured_in_two_stages_and_the_second_does_not_rewrite_the_first():
    started = manifest.begin(
        kind="proposal",
        mode="real",
        settings=ai_settings(),
        dataset={"items": 16},
        expected_items=16,
    )
    for field in started["captured_at_start"]:
        assert field in started, field
    for field in (
        "run_id",
        "started_utc",
        "evaluator_version",
        "expected_items",
        "retry_and_repair",
        "models_requested",
    ):
        assert field in started["captured_at_start"]
    assert started["evaluator_version"] == manifest.EVALUATOR_VERSION == "eval-3"
    assert started["retry_and_repair"]["truncated_output_retried"] is False
    assert (
        started["retry_and_repair"]["max_attempts_per_call"] == ai_settings().ai_retry_max_attempts
    )
    assert (
        "finished_utc" not in started and "cost" not in started
    )  # nothing from the end exists yet


async def test_finishing_adds_the_completion_fields_and_leaves_the_start_fields_alone(
    sessionmaker, alice
):
    settings = ai_settings(**UNPRICED)
    m = manifest.begin(
        kind="answer", mode="real", settings=settings, dataset={"questions": 2}, expected_items=2
    )
    before = copy.deepcopy({k: m[k] for k in m["captured_at_start"]})
    for status, code, tokens in (("ok", None, 120), ("failed", "provider_output_truncated", 138)):
        await execute(
            sessionmaker,
            "INSERT INTO ai_usage_ledger (user_id, charged_user_id, provider, model, operation, input_tokens, "
            "output_tokens, est_cost_micros, key_source, status, error_code) VALUES (:u, :u, 'openai', 'gpt-x', "
            "'classify', :t, 5, 0, 'server', :s, :c)",
            u=alice.id,
            t=tokens,
            s=status,
            c=code,
        )
    done = await manifest.finish(
        m,
        settings=settings,
        sessionmaker=sessionmaker,
        records=[{"id": "q1", "published": True}],
        results={"x": 1},
    )
    assert {k: done[k] for k in done["captured_at_start"] if k != "started_utc"} == {
        k: v for k, v in before.items() if k != "started_utc"
    }
    assert done["started_utc"] == m["started_utc"] and "finished_utc" in done
    assert done["provider_failures"]["by_operation_and_code"] == [
        {"operation": "classify", "error_code": "provider_output_truncated", "calls": 1}
    ]
    assert done["cost"]["status"] == "unpriced" and done["cost"]["est_cost_micros"] is None
    assert done["models_recorded"] == ["openai:gpt-x"]
    assert done["usage_totals"]["by_provider"]["openai"] == {
        "calls": 2,
        "failed_calls": 1,
        "input_tokens": 258,
        "output_tokens": 10,
    }


# ---- the runner reports the evaluator's own exit status --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "outcome"),
    [(0, "passed"), (1, "tests failed"), (5, "no tests collected"), (7, "other status")],
)
def test_the_runner_returns_and_records_exactly_the_evaluators_exit_status(tmp_path, code, outcome):
    status = run_eval.run(
        "answer",
        command=[sys.executable, "-c", f"print('evaluator output'); raise SystemExit({code})"],
        results_dir=tmp_path,
    )
    assert status == code  # a failing evaluator is never reported as success
    [record_file] = list(tmp_path.glob("run-status-answer-*.json"))
    record = json.loads(record_file.read_text(encoding="utf-8"))
    assert record["exit_status"] == code and outcome in record["meaning"]
    assert "not that of any command that ran after it" in record["note"]


def test_the_runner_refuses_an_unknown_target():
    assert run_eval.main(["nonsense"]) == 4 and run_eval.main([]) == 4
