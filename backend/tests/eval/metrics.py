"""Pure metric functions for the evaluation harness (tested in the normal suite by
tests/test_eval_metrics.py; nothing here touches a provider or a database)."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

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
