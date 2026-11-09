"""The pure metric functions of the evaluation harness (tests/eval/metrics.py)."""

import pytest

from tests.eval.metrics import (
    character_accuracy,
    contains,
    edit_distance,
    mean,
    normalize,
    percentile,
    recall_at_k,
    share,
)


def test_normalize_and_contains_ignore_case_and_spacing():
    assert normalize("  The  Warehouse\n processed ") == "the warehouse processed"
    assert contains("The WAREHOUSE   processed 12 orders", "warehouse processed")
    assert not contains("nothing here", "warehouse")


@pytest.mark.parametrize(
    ("a", "b", "d"),
    [("", "", 0), ("abc", "", 3), ("", "abc", 3), ("kitten", "sitting", 3), ("same", "same", 0)],
)
def test_edit_distance(a, b, d):
    assert edit_distance(a, b) == d


def test_character_accuracy():
    assert character_accuracy("the quick fox", "The  quick   fox") == 1.0
    assert character_accuracy("abcd", "abxd") == 0.75
    assert character_accuracy("abcd", "") == 0.0
    assert character_accuracy("ab", "zzzzzzzz") == 0.0  # floored, never negative
    assert character_accuracy("", "") == 1.0 and character_accuracy("", "x") == 0.0


def test_recall_at_k_counts_needles_found_in_the_first_k_passages():
    passages = ["alpha beta", "gamma delta", "epsilon"]
    assert recall_at_k(passages, ["alpha", "gamma"], 1) == 0.5
    assert recall_at_k(passages, ["alpha", "gamma"], 2) == 1.0
    assert recall_at_k(passages, ["epsilon", "missing"], 3) == 0.5
    assert recall_at_k(passages, [], 3) is None
    assert recall_at_k([], ["alpha"], 3) == 0.0


def test_percentile_is_nearest_rank():
    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(values, 50) == 3.0
    assert percentile(values, 95) == 5.0
    assert percentile(values, 0) == 1.0
    assert percentile([], 50) is None


def test_share_ignores_not_applicable_flags():
    assert share([True, False, None, True]) == pytest.approx(2 / 3)
    assert share([None, None]) is None and share([]) is None
    assert mean([1.0, 3.0]) == 2.0 and mean([]) is None
