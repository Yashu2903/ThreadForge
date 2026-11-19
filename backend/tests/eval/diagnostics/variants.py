"""Diagnostic request variants for the classifier repeat check. NOT part of the acceptance dataset.

These are extra instructions used ONLY by `classifier_repeat.py`. They are never sent through the answer evaluation, never
change `corpus.GOLD`, and their expected routes are not gold answers: they are the labels I wrote down BEFORE the repeat check
ran, from the classifier prompt's own definitions (`app/ai/answer/prompts.py` CLASSIFY_SYSTEM):

  qa              a question to answer from the material
  summarize       asks for a summary or overview of one or more files
  compare         asks to compare or contrast two or more files
  document_review asks to check, review, proofread or critique the open document

The prompt has no diagram route. A diagram request is neither a summary, a comparison, nor a review of the open document, so by
exclusion it is `qa` (the same reading the gold set uses for `diagram-release` and `diagram-architecture`); the evaluation's
`EXPECTED_TASK` maps `diagram` to `qa` for the same reason. That is a reading of an under-specified prompt, not a fact the text
states, and the report says so.

Editing this file changes a diagnostic, not the acceptance dataset. Its hash is recorded with every run.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    id: str
    expected: str  # the route written down before the check ran
    instruction: str
    why: str


VARIANTS: tuple[Variant, ...] = (
    Variant(
        "var-qa-owner",
        "qa",
        "Who is responsible for the deploy step in the release notes?",
        "a direct question",
    ),
    Variant(
        "var-qa-count", "qa", "How many parcels did the Lisbon depot handle?", "a direct question"
    ),
    Variant(
        "var-sum-overview",
        "summarize",
        "Give me a short overview of the release notes.",
        "asks for an overview of a file",
    ),
    Variant(
        "var-sum-bullets",
        "summarize",
        "Summarize the depot report in three bullet points.",
        "asks for a summary of a file",
    ),
    Variant(
        "var-cmp-differences",
        "compare",
        "What are the differences between the depot report and the release process?",
        "asks to contrast two files",
    ),
    Variant(
        "var-rev-proofread",
        "document_review",
        "Proofread this document and point out anything unclear.",
        "asks to proofread the open document",
    ),
    Variant(
        "var-rev-critique",
        "document_review",
        "Critique the plan I have open and tell me what is weak.",
        "asks to critique the open document",
    ),
    Variant(
        "var-dia-deploy",
        "qa",
        "Show the deployment steps as a flow diagram.",
        "a diagram request: not a summary, comparison or document review",
    ),
    Variant(
        "var-dia-release",
        "qa",
        "Make a flowchart of how a release goes out.",
        "a diagram request: not a summary, comparison or document review",
    ),
    Variant(
        "var-dia-system",
        "qa",
        "Sketch a diagram of the system described in the handbook.",
        "a diagram request: not a summary, comparison or document review",
    ),
)

# the gold instructions the check reuses (read from corpus.GOLD, never copied): one or two per route
GOLD_IDS: tuple[str, ...] = (
    "qa-berlin",
    "qa-md-owner",
    "figure-handbook",
    "summarize-release",
    "compare-depots-release",
    "review-launch-plan",
    "diagram-release",
    "diagram-architecture",
)
