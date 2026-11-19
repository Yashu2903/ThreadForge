"""Pure metric functions for the evaluation harness (tested in the normal suite by
tests/test_eval_metrics.py; nothing here touches a provider or a database)."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lower case, one space between words. Used to compare OCR output and to find needles."""
    return _SPACE.sub(" ", text.lower()).strip()


def contains(text: str, needle: str) -> bool:
    return normalize(needle) in normalize(text)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (insert, delete, substitute; each cost 1)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def character_accuracy(truth: str, got: str) -> float:
    """1 - (edit distance / length of the truth), on normalized text, floored at 0. 1.0 is a perfect read."""
    t, g = normalize(truth), normalize(got)
    if not t:
        return 1.0 if not g else 0.0
    return max(0.0, 1.0 - edit_distance(t, g) / len(t))


def recall_at_k(ranked_texts: Sequence[str], needles: Iterable[str], k: int) -> float | None:
    """Share of the needles found in the first k ranked passages. None when there is nothing to find."""
    wanted = list(needles)
    if not wanted:
        return None
    top = ranked_texts[:k]
    found = sum(1 for n in wanted if any(contains(t, n) for t in top))
    return found / len(wanted)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def share(flags: Iterable[bool | None]) -> float | None:
    """Fraction of True among the non-None flags; None when no flag applies."""
    applicable = [f for f in flags if f is not None]
    if not applicable:
        return None
    return sum(1 for f in applicable if f) / len(applicable)


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def fraction(numerator: int, denominator: int) -> dict[str, Any]:
    """A ratio that always carries its numerator and denominator; the value is None for an empty denominator."""
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def routing_metrics(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Routing outcomes of the answer evaluation, each with its denominator. A record needs `published`, `expected_route`
    and, when it was routed, `routing` (the published coverage.routing). Definitions:

      requests                       every gold item sent
      routed                         published requests (they carry a routing record)
      classification attempts        routed requests whose hint asked the model to classify
      valid model classifications    attempts where the model returned one of the routes
      model accuracy (of valid)      valid classifications equal to the expected route / valid classifications
      model accuracy (of attempts)   the same numerator / ALL attempts: a failed classification counts as not correct
      fallback requests              routed requests that ran the fallback because the classifier failed
      final route accuracy           routed requests whose FINAL route equals the expected one, fallback included
      final correct by fallback      the part of that numerator reached only because the classifier FAILED

    A request that is right because of the fallback adds to the failure count, the fallback count and final accuracy;
    it never adds to the model classification counts."""
    published = [r for r in records if r.get("published")]
    routed = [r for r in published if r.get("routing")]
    attempts = [r for r in routed if r["routing"]["classification_attempted"]]
    valid = [r for r in attempts if r["routing"]["model_route"] is not None]
    failed = [r for r in attempts if r["routing"]["model_route"] is None]
    model_right = [r for r in valid if r["routing"]["model_route"] == r["expected_route"]]
    fallback = [r for r in routed if r["routing"]["fallback_used"]]
    final_right = [r for r in routed if r["routing"]["final_route"] == r["expected_route"]]
    return {
        "published": fraction(len(published), len(records)),
        "classification_attempts": len(attempts),
        "valid_model_classifications": fraction(len(valid), len(attempts)),
        "model_accuracy_of_valid": fraction(len(model_right), len(valid)),
        "model_accuracy_of_attempts": fraction(len(model_right), len(attempts)),
        "classifier_failures": fraction(len(failed), len(attempts)),
        "classifier_failures_by_category": dict(
            Counter(r["routing"]["failure"] or "unknown" for r in failed)
        ),
        "fallback_requests": fraction(len(fallback), len(routed)),
        "final_route_accuracy": fraction(len(final_right), len(routed)),
        "final_correct_by_fallback": sum(1 for r in final_right if r["routing"]["fallback_used"]),
        "model_misrouted": [
            {
                "id": r["id"],
                "model_route": r["routing"]["model_route"],
                "expected": r["expected_route"],
            }
            for r in valid
            if r["routing"]["model_route"] != r["expected_route"]
        ],
        "fallback_ids": [r["id"] for r in fallback],
    }


def failed_calls(ledger_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Provider calls that did not succeed, by operation and stable error code, from the run-wide usage ledger.
    A request can publish in spite of them, so this is reported on its own, never folded into a success rate."""
    counts: Counter[tuple[str, str]] = Counter()
    total = failed = 0
    for r in ledger_rows:
        n = int(r["calls"])
        total += n
        if r["status"] != "ok":
            failed += n
            counts[(r["operation"], r.get("error_code") or "-")] += n
    return {
        "calls": fraction(failed, total),
        "by_operation_and_code": [
            {"operation": op, "error_code": code, "calls": n}
            for (op, code), n in sorted(counts.items())
        ],
    }
