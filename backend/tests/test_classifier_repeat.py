"""The classifier repeat check (a diagnostic) and the classifier effort in the shipped example configuration.

No provider is called here. These tests pin three things:
  * the diagnostic cannot leak into the acceptance dataset (its variants are separate, and the gold set is only READ);
  * its summary and decision logic count what they say they count (numerators over denominators, a failure is never a hit);
  * the example configuration's classifier setting is one the application accepts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.ai.answer.execute import ROUTES
from tests.ai_helpers import ai_settings
from tests.eval import corpus
from tests.eval.diagnostics import classifier_repeat as cr
from tests.eval.diagnostics.variants import GOLD_IDS, VARIANTS

REPO = Path(__file__).resolve().parents[2]


def test_variants_are_separate_from_the_acceptance_dataset():
    gold_ids = {g.id for g in corpus.GOLD}
    gold_text = {g.instruction.strip().lower() for g in corpus.GOLD}
    assert not gold_ids & {v.id for v in VARIANTS}
    assert not gold_text & {v.instruction.strip().lower() for v in VARIANTS}
    assert set(GOLD_IDS) <= gold_ids  # the gold items the check reuses exist; it only reads them
    assert all(v.expected in ROUTES for v in VARIANTS)


def test_the_check_reads_gold_without_changing_it():
    before = [(g.id, g.task, g.instruction, g.needles, g.cites) for g in corpus.GOLD]
    listed = cr.items()
    assert [(g.id, g.task, g.instruction, g.needles, g.cites) for g in corpus.GOLD] == before
    by_id = {i["id"]: i for i in listed}
    # the expected route of a gold item is the evaluation's own mapping, not a value chosen here
    assert by_id["diagram-release"]["expected"] == "qa"
    assert by_id["review-launch-plan"]["expected"] == "document_review"
    assert {i["source"] for i in listed} == {"gold", "variant"}


def test_every_route_the_check_expects_is_a_real_route():
    assert {i["expected"] for i in cr.items()} <= set(ROUTES)
    assert set(ROUTES) <= {
        i["expected"] for i in cr.items()
    }  # and every supported route is exercised


async def test_the_call_count_is_capped_before_anything_is_built_or_sent(monkeypatch):
    def not_reached(**_: object):
        pytest.fail("settings or a provider client was built before the call cap was checked")

    monkeypatch.setattr(cr, "make_settings", not_reached)
    monkeypatch.setattr(cr, "build_llm", not_reached)
    too_many = cr.MAX_CALLS // (len(cr.items()) * len(cr.ARMS)) + 1
    with pytest.raises(SystemExit, match="exceed the cap"):
        await cr.run(too_many)
    # the default the module ships with is inside the cap
    assert len(cr.items()) * len(cr.ARMS) * 5 <= cr.MAX_CALLS


def _rows(spec):
    """spec: {(id, arm): [route or None, ...]} -> attempt rows"""
    rows = []
    for (iid, arm), routes in spec.items():
        for n, route in enumerate(routes, 1):
            rows.append(
                {
                    "id": iid,
                    "arm": arm,
                    "attempt": n,
                    "route": route,
                    "failure": None if route else "incomplete",
                    "in": 10,
                    "out": 5,
                    "seconds": 1.0,
                }
            )
    return rows


def _todo():
    return [
        {"id": "diagram-release", "source": "gold", "instruction": "x", "expected": "qa"},
        {"id": "qa-berlin", "source": "gold", "instruction": "y", "expected": "qa"},
        {"id": "summarize-release", "source": "gold", "instruction": "z", "expected": "summarize"},
    ]


def test_summary_counts_correct_attempts_over_attempts_and_a_failure_is_not_a_hit():
    todo = _todo()
    rows = _rows(
        {
            ("diagram-release", "default"): ["summarize", "summarize", "qa", None],
            ("diagram-release", "none"): ["qa", "qa", "qa", "qa"],
            ("qa-berlin", "default"): ["qa"] * 4,
            ("qa-berlin", "none"): ["qa"] * 4,
            ("summarize-release", "default"): ["summarize"] * 4,
            ("summarize-release", "none"): ["summarize"] * 4,
        }
    )
    s = cr.summarise(todo, rows, ai_settings())
    d = s["per_item"]["diagram-release"]["default"]
    assert (d["correct"], d["attempts"], d["failures"]) == (1, 4, 1)
    assert d["routes"] == {"summarize": 2, "qa": 1, "failure:incomplete": 1}
    assert s["per_route"]["qa"]["default"] == {"attempts": 8, "correct": 5, "failures": 1}
    assert s["per_route"]["qa"]["none"] == {"attempts": 8, "correct": 8, "failures": 0}
    assert s["rule"]["rules_2_to_4_hold"] is True


def test_the_decision_rule_rejects_each_way_none_can_be_worse():
    todo = _todo()
    good = {
        ("qa-berlin", "default"): ["qa"] * 3,
        ("summarize-release", "default"): ["summarize"] * 3,
        ("summarize-release", "none"): ["summarize"] * 3,
        ("diagram-release", "default"): ["qa"] * 3,
        ("diagram-release", "none"): ["qa"] * 3,
        ("qa-berlin", "none"): ["qa"] * 3,
    }
    assert cr.summarise(todo, _rows(good), ai_settings())["rule"]["rules_2_to_4_hold"] is True

    # rule 2: diagram-release not right every time under none
    r2 = {**good, **{("diagram-release", "none"): ["qa", "qa", "summarize"]}}
    out = cr.summarise(todo, _rows(r2), ai_settings())["rule"]
    assert out["rule2_diagram_release_all_correct_under_none"] is False
    assert out["rules_2_to_4_hold"] is False

    # rule 3 and 4: a route that is worse under none (and a gold item right by default, wrong under none)
    r3 = {**good, **{("summarize-release", "none"): ["summarize", "qa", "summarize"]}}
    out = cr.summarise(todo, _rows(r3), ai_settings())["rule"]
    assert out["rule3_no_route_worse_and_no_failures"] is False
    assert out["rule4_gold_items_worse_under_none"] == ["summarize-release"]
    assert out["rules_2_to_4_hold"] is False

    # rule 3: a failure under none is never acceptable, even when the correct counts TIE (default has one wrong answer too)
    r3b = {
        **good,
        ("qa-berlin", "default"): ["qa", "qa", "summarize"],
        ("qa-berlin", "none"): ["qa", "qa", None],
    }
    out = cr.summarise(todo, _rows(r3b), ai_settings())
    qa_none = out["per_route"]["qa"]["none"]
    qa_default = out["per_route"]["qa"]["default"]
    assert qa_none["correct"] >= qa_default["correct"] and qa_none["failures"] == 1
    assert out["rule"]["rule3_no_route_worse_and_no_failures"] is False


def test_the_report_says_cost_is_unknown_when_no_prices_are_configured():
    todo = _todo()
    rows = _rows({(i["id"], arm): [i["expected"]] * 2 for i in todo for arm in ("default", "none")})
    unpriced = ai_settings(
        ai_llm_input_micros_per_mtok=None,
        ai_llm_output_micros_per_mtok=None,
        ai_embed_text_micros_per_mtok=None,
        ai_embed_image_micros_per_mtok=None,
        ai_prices_checked_on="",
    )
    s = cr.summarise(todo, rows, unpriced)
    assert all(a["cost"]["cost_status"] == "unpriced" for a in s["per_arm"].values())
    assert all(a["cost"]["est_cost_micros"] is None for a in s["per_arm"].values())
    result = {
        "start_stage": {
            "config": {"ai_llm_model": "m"},
            "classifier_output_budget": 512,
            "repeats": 2,
            "planned_calls": 12,
            "call_cap": 200,
        },
        "summary": s,
    }
    text = cr.render(result)
    assert "unknown (no prices configured)" in text
    assert "0 micros" not in text


# ---- the shipped example configuration ----------------------------------------------------------------------------------


def _example_value(name: str) -> str | None:
    """The UNCOMMENTED value of `name` in .env.example, or None when it is only a comment."""
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^{name}=([^#\s]*)", line)
        if m:
            return m.group(1)
    return None


def test_the_example_configuration_sets_the_classifier_effort_the_app_accepts():
    effort = _example_value("AI_CLASSIFIER_REASONING_EFFORT")
    # adopted for gpt-6-luna after the 13-question run and the repeat check (docs/ai.md 23)
    assert effort == "none"
    tokens = int(_example_value("AI_CLASSIFIER_MAX_OUTPUT_TOKENS") or 0)
    # the exact pair the example ships must pass the application's own validation
    s = ai_settings(ai_classifier_reasoning_effort=effort, ai_classifier_max_output_tokens=tokens)
    assert (s.ai_classifier_reasoning_effort, s.ai_classifier_max_output_tokens) == ("none", 512)


def test_the_code_default_stays_model_agnostic():
    """The example sets `none` for the documented model; the code default stays empty so another model keeps working."""
    assert ai_settings().ai_classifier_reasoning_effort == ""
