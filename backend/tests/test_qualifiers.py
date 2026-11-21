"""The qualification check of rewrites (app/ai/edit/qualifiers.py): pairs of texts, each rule with a valid paraphrase
(no flag) beside a real change (a flag), because a rule that flags every rewording would be as useless as one that flags
nothing. Pure functions: no database, no provider."""

from __future__ import annotations

import pytest

from app.ai.edit.qualifiers import compare


def codes(before: str, after: str) -> list[str]:
    return [f.code for f in compare(before, after)]


LAUNCH = "The launch is scheduled for Friday at noon."


def test_the_p5_regression_a_scheduled_launch_must_not_become_a_fact():
    # the reviewed output: "scheduled" dropped, so a plan reads as something that is simply true
    assert codes(LAUNCH, "Launch is Friday at noon.") == ["status_changed"]
    flag = compare(LAUNCH, "Launch is Friday at noon.")[0]
    assert "scheduled" in flag.detail and "planned" in flag.detail


@pytest.mark.parametrize(
    "after",
    [
        "Launch is scheduled for Friday at noon.",  # the concise rewrite the review accepted
        "The launch is scheduled to take place at noon on Friday.",  # P1: wordier, same meaning
        "Launch is planned for Friday, 12:00.",  # a synonym of the same class, the time written another way
        "We will launch on Friday at noon.",  # "will" is the same class: a plan
        "Friday noon: launch (scheduled).",  # order changed
    ],
)
def test_valid_rewordings_of_the_launch_sentence_are_not_flagged(after):
    assert codes(LAUNCH, after) == []


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("The launch is scheduled for Friday.", ["figure_changed"]),  # the time was lost
        ("The launch is scheduled for Thursday at noon.", ["figure_changed"]),  # the day changed
        ("The launch is scheduled for Friday at 3 pm.", ["figure_changed"]),  # the time changed
        (
            "The launch is complete on Friday at noon.",
            ["status_changed"],
        ),  # planned became completed
        ("The launch is guaranteed for Friday at noon.", ["status_changed", "strengthened"]),
    ],
)
def test_real_changes_to_the_launch_sentence_are_flagged(after, expected):
    assert codes(LAUNCH, after) == expected


RISK = "Vendor delays could slip the schedule."


def test_hedges_a_synonym_is_fine_dropping_the_hedge_is_not():
    assert codes(RISK, "Vendor delays may affect the schedule.") == []  # could -> may: both hedges
    assert codes(RISK, "Delays from vendors might slip the schedule.") == []
    assert codes(RISK, "Vendor delays will slip the schedule.") == ["hedge_dropped"]
    assert codes(RISK, "Vendor delays slip the schedule.") == ["hedge_dropped"]


def test_may_the_month_is_not_a_hedge():
    # "in May" is a date: dropping it is a figure change, not a dropped hedge
    assert codes("Revenue was reported in May.", "Revenue was reported in June.") == [
        "figure_changed"
    ]
    assert codes("The deploy may fail.", "The deploy fails.") == ["hedge_dropped"]


def test_negation_in_either_direction():
    assert codes("The budget is not approved.", "The budget is approved.") == ["negation_changed"]
    assert codes("The budget is approved.", "The budget is not approved.") == ["negation_changed"]
    assert codes("There is no owner yet.", "No one owns it yet.") == []  # still negative


def test_conditions_and_dependencies():
    original = "We ship on Friday unless QA finds a blocker."
    assert (
        codes(original, "We ship on Friday unless QA finds a problem.") == []
    )  # still conditional
    # a known false positive, kept here on purpose: the same condition written with the negation moved reads as
    # a negation change to a word-level check. A reviewer sees at once that it is equivalent; the flag is only a note.
    assert codes(original, "We ship on Friday if QA finds no blocker.") == ["negation_changed"]
    assert codes(original, "We ship on Friday.") == ["condition_dropped"]


def test_attribution_who_said_it():
    original = "According to finance, the budget is forty thousand dollars."
    assert codes(original, "Finance reports the budget is forty thousand dollars.") == []
    assert codes(original, "The budget is forty thousand dollars.") == ["attribution_dropped"]


@pytest.mark.parametrize(
    "after",
    [
        "The budget is $40,000.",
        "The budget is 40,000 dollars.",
        "The budget is 40 thousand dollars.",
    ],
)
def test_the_same_amount_in_another_notation_is_not_a_change(after):
    assert codes("The budget is forty thousand dollars.", after) == []


def test_a_changed_or_unitless_amount_is_flagged():
    before = "The budget is forty thousand dollars."
    assert codes(before, "The budget is fourteen thousand dollars.") == ["figure_changed"]
    assert codes(before, "The budget is 40,000.") == ["figure_changed"]  # the unit was lost


def test_times_noon_is_twelve_hundred_and_pm_is_converted():
    assert codes("Doors open at noon.", "Doors open at 12:00.") == []
    assert codes("Doors open at 3 pm.", "Doors open at 15:00.") == []
    assert codes("Doors open at noon.", "Doors open at 1 pm.") == ["figure_changed"]


def test_estimated_and_expected_are_not_the_same_as_measured():
    assert codes("The cost is estimated at 500 dollars.", "The cost is 500 dollars.") == [
        "hedge_dropped",
        "status_changed",
    ]
    assert (
        codes("The cost is estimated at 500 dollars.", "The cost is projected at 500 dollars.")
        == []
    )


def test_a_pronoun_one_is_not_a_figure():
    assert codes("One of the owners left.", "An owner left.") == []


def test_unchanged_text_has_no_flags_and_empty_text_is_safe():
    assert compare(LAUNCH, LAUNCH) == []
    assert compare("", "") == []
    assert compare(LAUNCH, "") != []  # everything was removed: that is flagged, not an error


def test_the_detail_names_what_changed_in_words_a_reviewer_can_read():
    flag = compare("The cost is estimated at 500 dollars.", "The cost is 600 dollars.")[-1]
    assert flag.code == "figure_changed"
    assert "500" in flag.detail and "600" in flag.detail


def test_a_negative_contraction_is_the_same_as_the_words_it_stands_for():
    # seen in the held-out evaluation: "will not resume" rewritten as "won't resume" was flagged as a dropped plan
    before = "Deliveries will not resume until the strike ends, which may take two weeks."
    assert (
        codes(before, "Deliveries won't resume until the strike ends, which may take two weeks.")
        == []
    )
    assert codes("The vendor is not ready.", "The vendor isn't ready.") == []
    assert codes("We cannot ship on Friday.", "We can't ship on Friday.") == []
    # and the contraction does not hide a real change: the negation really is gone
    assert codes("Deliveries won't resume this week.", "Deliveries will resume this week.") == [
        "negation_changed"
    ]
