"""Did a rewrite keep the qualifications of the text it replaced? A deterministic, conservative review aid.

WHAT THIS IS. A rewrite can be perfectly valid (the schema, the labels and the document check all pass) and still say
more, or less, than the original: "The launch is scheduled for Friday at noon." becomes "Launch is Friday at noon.", a
plan turned into a statement of fact. Nothing else in the pipeline looks at that. This module compares the text a
change removes with the text it writes and names the qualifications that did not survive:

    hedge_dropped         a hedge ("could", "approximately", "estimated") is gone
    status_changed        the state of the work changed or vanished (scheduled/planned, proposed, approved, completed)
    negation_changed      a negation appeared or disappeared ("not", "no", "never", "without")
    condition_dropped     a condition or dependency ("if", "unless", "once", "until") is gone
    attribution_dropped   who said or owns something ("according to", "said", "reported") is gone
    figure_changed        a number, amount, date, time or weekday was removed, added or altered
    strengthened          an absolute ("always", "definitely", "guaranteed", "all") that the original did not have

WHAT IT IS NOT. It is a word-level check, not a proof of meaning. It does not understand a sentence: it cannot see that
two different words are synonyms unless they are listed here, so it can miss a real change (a hedge replaced by a word
of a different strength) and it can flag a harmless one. That is why a flag is a REVIEW NOTE shown on the change in the
preview, never a rejection and never a reason to spend another model call: a person decides. A change with no flag has
not been shown to be safe; it only passed these checks. Rewording that keeps every qualification (a shorter sentence, a
different order, a word of the same class) is not flagged.

Each list is a set of words that mean the same thing for this purpose: the status class "planned" holds scheduled,
planned, slated, due, intended and will, so swapping one for another is not a change, and dropping all of them is.
Extending a list is a data change; tests/test_qualifiers.py holds a valid paraphrase and a real change for each rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d[\d,]*(?:\.\d+)?|[$€%]")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?|\b(\d{1,2})\s*(am|pm)\b")

HEDGES = frozenset(
    [
        "may",
        "might",
        "could",
        "possibly",
        "perhaps",
        "probably",
        "likely",
        "unlikely",
        "approximately",
        "roughly",
        "about",
        "around",
        "estimated",
        "tentative",
        "tentatively",
        "preliminary",
        "provisional",
        "unconfirmed",
        "apparently",
        "reportedly",
        "allegedly",
        "seems",
        "appears",
        "suggests",
        "potentially",
        "potential",
        "expected",
        "anticipated",
        "projected",
        "nearly",
        "almost",
    ]
)
# a month is not a hedge: "may" counts as one unless it reads as the month
_MONTH_CONTEXT = frozenset(
    ["in", "of", "by", "since", "during", "until", "early", "late", "mid", "before", "after"]
)

# the state of the work; the words of one class are interchangeable
STATUS: dict[str, frozenset[str]] = {
    "planned": frozenset(["scheduled", "planned", "slated", "due", "intended", "will"]),
    "proposed": frozenset(["proposed", "suggested", "draft", "pending"]),
    "approved": frozenset(
        ["approved", "confirmed", "authorised", "authorized", "agreed", "signed"]
    ),
    "completed": frozenset(
        [
            "completed",
            "done",
            "finished",
            "delivered",
            "shipped",
            "released",
            "launched",
            "complete",
        ]
    ),
    "started": frozenset(["started", "begun", "ongoing", "underway"]),
    "estimated": frozenset(["estimated", "forecast", "projected", "expected"]),
    "cancelled": frozenset(
        ["cancelled", "canceled", "postponed", "delayed", "abandoned", "withdrawn"]
    ),
}
NEGATIONS = frozenset(
    [
        "not",
        "no",
        "never",
        "none",
        "cannot",
        "without",
        "neither",
        "nor",
        "nothing",
        "nobody",
        "n't",
    ]
)
CONDITIONS = frozenset(
    [
        "if",
        "unless",
        "once",
        "until",
        "provided",
        "whenever",
        "only",
        "depends",
        "dependent",
        "contingent",
    ]
)
ATTRIBUTION = frozenset(
    [
        "according",
        "said",
        "says",
        "stated",
        "states",
        "reported",
        "reports",
        "report",
        "claims",
        "claimed",
        "announced",
        "noted",
        "notes",
        "wrote",
        "writes",
        "told",
        "tells",
    ]
)
ABSOLUTES = frozenset(
    [
        "always",
        "definitely",
        "certainly",
        "guaranteed",
        "all",
        "every",
        "entirely",
        "completely",
        "undoubtedly",
    ]
)

_WEEKDAYS = frozenset(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
)
_MONTHS = frozenset(
    [
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
)
_NUMBER_WORDS = {
    **{
        w: i
        for i, w in enumerate(
            [
                "zero",
                "one",
                "two",
                "three",
                "four",
                "five",
                "six",
                "seven",
                "eight",
                "nine",
                "ten",
                "eleven",
                "twelve",
                "thirteen",
                "fourteen",
                "fifteen",
                "sixteen",
                "seventeen",
                "eighteen",
                "nineteen",
            ]
        )
    },
    **{
        w: 10 * (i + 2)
        for i, w in enumerate(
            ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
        )
    },
}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_UNITS = {
    "dollar": "usd", "dollars": "usd", "usd": "usd", "$": "usd", "euro": "eur", "euros": "eur", "eur": "eur",
    "€": "eur", "percent": "%", "%": "%", "day": "day", "days": "day", "week": "week", "weeks": "week",
    "month": "month", "months": "month", "hour": "hour", "hours": "hour", "minute": "minute", "minutes": "minute",
    "people": "person", "person": "person", "parcel": "parcel", "parcels": "parcel",
}  # fmt: skip


@dataclass(frozen=True)
class Flag:
    code: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("’", "'"))


def _words(tokens: list[str]) -> set[str]:
    out = set(tokens)
    out |= {"n't" for t in tokens if t.endswith("n't")}
    return out


def _hedges(tokens: list[str]) -> set[str]:
    found = set()
    for i, t in enumerate(tokens):
        if t not in HEDGES:
            continue
        if t == "may":
            prev = tokens[i - 1] if i else ""
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if prev in _MONTH_CONTEXT or nxt[:1].isdigit():
                continue  # the month
        found.add(t)
    return found


def _status(tokens: list[str]) -> set[str]:
    words = set(tokens)
    found = {name for name, members in STATUS.items() if words & members}
    if any(
        a == "set" and b in ("for", "to", "on") for a, b in zip(tokens, tokens[1:], strict=False)
    ):
        found.add("planned")
    return found


def figures(text: str) -> set[str]:
    """Numbers, amounts, times and dates as normalized strings: '$40,000', '40000 dollars' and 'forty thousand dollars'
    are one figure, and 'noon' is '12:00'."""
    raw = text.lower().replace("’", "'")
    out: set[str] = set()

    def times(m: re.Match[str]) -> str:
        if m.group(1) is not None:
            hour, minute, ap = int(m.group(1)), m.group(2), m.group(3)
        else:
            hour, minute, ap = int(m.group(4)), "00", m.group(5)
        if ap == "pm" and hour < 12:
            hour += 12
        if ap == "am" and hour == 12:
            hour = 0
        out.add(f"time {hour:02d}:{minute}")
        return " "

    raw = _TIME.sub(times, raw)
    toks = _tokens(raw)
    for t in toks:
        if t in _WEEKDAYS:
            out.add(f"day {t}")
        elif t in _MONTHS:
            out.add(f"month {t}")
        elif t in ("noon", "midday"):
            out.add("time 12:00")
        elif t == "midnight":
            out.add("time 00:00")
    i = 0
    while i < len(toks):
        t = toks[i]
        start, value = i, None
        if t[:1].isdigit():
            try:
                value = float(t.replace(",", ""))
            except ValueError:
                value = None
            i += 1
            if i < len(toks) and toks[i] in _SCALES and value is not None:
                value *= _SCALES[toks[i]]
                i += 1
        elif t in _NUMBER_WORDS or t in _SCALES:
            total = current = 0
            j = i
            while j < len(toks) and (
                toks[j] in _NUMBER_WORDS
                or toks[j] in _SCALES
                or (
                    toks[j] == "and"
                    and j + 1 < len(toks)
                    and (toks[j + 1] in _NUMBER_WORDS or toks[j + 1] in _SCALES)
                )
            ):
                w = toks[j]
                if w in _NUMBER_WORDS:
                    current += _NUMBER_WORDS[w]
                elif w in _SCALES:
                    current = (current or 1) * _SCALES[w]
                    if _SCALES[w] >= 1000:
                        total, current = total + current, 0
                j += 1
            nxt = toks[j] if j < len(toks) else ""
            if t == "one" and j == i + 1 and nxt not in _UNITS and nxt not in _SCALES:
                i += 1  # "one of the ..." is a pronoun, not a count
                continue
            value, i = float(total + current), j
        else:
            i += 1
            continue
        if value is None:
            continue
        prev = toks[start - 1] if start else ""
        nxt = toks[i] if i < len(toks) else ""
        unit = _UNITS.get(prev) if prev in ("$", "€") else _UNITS.get(nxt, "")
        number = str(int(value)) if float(value).is_integer() else str(value)
        out.add(f"{number} {unit}".strip())
    return out


def compare(before: str, after: str) -> list[Flag]:
    """The qualifications of `before` that `after` lost or changed. Empty when they all survived."""
    bt, at = _tokens(before), _tokens(after)
    bw, aw = _words(bt), _words(at)
    flags: list[Flag] = []

    b_hedge, a_hedge = _hedges(bt), _hedges(at)
    if b_hedge and not a_hedge and not (_status(at) & {"estimated"}):
        flags.append(
            Flag("hedge_dropped", f"“{sorted(b_hedge)[0]}” is gone: the statement is now firmer")
        )

    b_status, a_status = _status(bt), _status(at)
    if b_status and not (b_status & a_status):
        lost = sorted(b_status)[0]
        word = sorted(bw & STATUS[lost])[0] if bw & STATUS[lost] else lost
        if a_status:
            flags.append(Flag("status_changed", f"“{word}” ({lost}) became {sorted(a_status)[0]}"))
        else:
            flags.append(
                Flag(
                    "status_changed",
                    f"“{word}” ({lost}) is gone: the text no longer says the work is {lost}",
                )
            )

    if bool(bw & NEGATIONS) != bool(aw & NEGATIONS):
        flags.append(
            Flag(
                "negation_changed",
                "a negation was removed" if bw & NEGATIONS else "a negation was added",
            )
        )

    b_cond = bw & CONDITIONS
    if b_cond and not (aw & CONDITIONS):
        flags.append(Flag("condition_dropped", f"“{sorted(b_cond)[0]}” (a condition) is gone"))

    b_attr = bw & ATTRIBUTION
    if b_attr and not (aw & ATTRIBUTION):
        flags.append(Flag("attribution_dropped", f"“{sorted(b_attr)[0]}” (who said it) is gone"))

    bf, af = figures(before), figures(after)
    missing, extra = sorted(bf - af), sorted(af - bf)
    if missing or extra:
        parts = []
        if missing:
            parts.append("no longer in the text: " + ", ".join(missing))
        if extra:
            parts.append("new in the text: " + ", ".join(extra))
        flags.append(Flag("figure_changed", "; ".join(parts)))

    new_absolute = (aw & ABSOLUTES) - bw
    if new_absolute:
        flags.append(
            Flag(
                "strengthened",
                f"“{sorted(new_absolute)[0]}” was added: the statement is now absolute",
            )
        )
    return flags
