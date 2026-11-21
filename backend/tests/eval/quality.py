"""The task-quality dataset (`quality-v1`) and its ACCEPTANCE CRITERIA, declared BEFORE the first run that uses them.

WHY A SECOND DATASET. The acceptance dataset (corpus.py: 13 questions; proposals.py: the schema gate) says whether the
pipeline publishes valid, cited output. It cannot say whether a summary says anything, whether a rewrite kept the meaning, or
whether a diagram admits what it assumes: those were the findings of the human review (docs/evidence/ai-eval/human-review-sheet.md).
This dataset asks those questions. It ADDS cases; the acceptance dataset and its gold expectations are untouched.

TWO SPLITS, never mixed in a number.
  dev       the cases whose failures shaped the prompts and the checks (the review-sheet examples and near relatives). Results on
            dev show the fixes work on what they were built from; they do not show they generalize.
  heldout   cases on DIFFERENT documents and sources, written before any model output on them was seen and not used to tune a
            prompt or a check. Only the held-out split can show the changes generalize. If a held-out result prompts a change,
            the change is made, the held-out set is spent, and the report says so.

WHAT A CHECK IS. Every check is deterministic and labelled (tests/eval/checks.py): structural (valid, rendered, cited), concept
presence against a written rubric, figures not found in the material, a placeholder diagnostic, whether the output SAID an order is
assumed, labels that may be invented. None of them is a human review and none is a judge. Semantic quality that a rule cannot decide
(is the summary faithful, is the rewrite clearer) is recorded as an AGENT ASSESSMENT, by hand, after the run, labelled as such. Nothing
here asks a model to grade a model.

THE ACCEPTANCE CRITERIA (fixed before the runs; the report states each as met or not met, with its numbers):

  REGRESSION GUARDS (unchanged rules, must still hold on the new build)
    G1  proposal schema gate: schema-valid with at most one repair >= 90% of the 14 proposal gold items (tests/eval/test_proposal_eval.py).
    G2  answer routing: final route correct on >= 12 of 13 and no classifier failure (tests/eval/test_eval.py, effort `none`).
    G3  no item that published in the baseline answer run fails to publish in the new one.

  THE FINDINGS (each: the new build's result on the HELD-OUT split; dev is reported beside it, never instead of it)
    F1  closing summary (P7): every closing-summary item carries >= 4 of its rubric concepts, with no figure absent from the document and
        no placeholder.   Not met when any item has fewer.
    F2  missing context (A5/brief 3): every missing-context item publishes with the `document_not_included` notice and sends no document
        text.
    F3  derived citations (A3/A7): 100% of derived citations of every published summary open to verified original text, and none has
        incomplete provenance.
    F4  diagrams (A6/A7/P4/P8): of the diagram items that DRAW an order, every one says the order is assumed or not stated (its own
        assumptions, a gap or its chat text), and no diagram names a step or stage the sources do not name.
    F5  rewrites (P5/P1): every sentence a rewrite changed keeps the facts of its rubric (concepts); and of the rewrites that LOST a
        qualification (a concept of the rubric), the app's own review flag marked at least 80% (the flag is a review aid: this measures
        how often it speaks when it should).
    F6  comparison and draft: every comparison cites both selected sources and mentions both in its text; every draft carries >= 4 of its
        rubric concepts without naming a third gate the source does not name.
    F7  document review wording (A5): a review does not say an effect is missing when the document states one, and names no risk the
        document does not state.

  HOW THE BASELINE IS USED. The same dataset and checks are run on the commit before these changes (0fe9a95) and on the new build; the
  difference per finding is the evidence that the change did something. One sample per item per run: with n this small a single flip is
  noise, so no finding is called fixed on dev alone, and a result is described as measured, not as proven.

The items below were written from the synthetic fixtures' own text, before any run on them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from tests.eval import corpus
from tests.eval.checks import Concept

DATASET_VERSION = "quality-v1"

# ---- fixtures: synthetic, public, byte-deterministic ---------------------------------------------------------

ONBOARDING_MD = (
    "# Onboarding\n\nNew hires complete several onboarding items.\n\n## Orientation\n\n- meet the team\n- tour the warehouse\n\n"
    "## Payroll\n\nPayroll setup is handled by Dana.\n\n## Equipment\n\n"
    "The IT desk issues laptops and badges. Equipment requests are approved by Sam.\n"
)

EXTRA_SOURCES = {"onboarding.md": ONBOARDING_MD.encode()}


def all_sources() -> dict[str, bytes]:
    return {**corpus.sources(), **EXTRA_SOURCES}


# ---- documents the edit items work on ----------------------------------------------------------------------------

ATLAS = [
    ("heading", 1, "Project Atlas brief"),
    (
        "paragraph",
        0,
        "Atlas moves the order system from the old data centre to the cloud by 30 June.",
    ),
    ("paragraph", 0, "The migration budget is 120,000 euros, approved by the steering group."),
    ("heading", 2, "Risks"),
    ("paragraph", 0, "The main risk is the payment gateway, which has no staging environment."),
    ("paragraph", 0, "Maria Keller owns the cutover plan; Tom Vogel owns the rollback plan."),
]
QUALIFIED_DEV = [
    ("heading", 1, "Status notes"),
    ("paragraph", 0, "The launch is scheduled for Friday at noon."),
    ("paragraph", 0, "Vendor delays could slip the schedule."),
    ("paragraph", 0, "The budget is estimated at forty thousand dollars and is not yet approved."),
    ("paragraph", 0, "Support will move to the new queue if the migration finishes by June."),
]
HIRING = [
    ("heading", 1, "Hiring memo"),
    (
        "paragraph",
        0,
        "The support team will hire three agents by 15 September, starting with the night shift.",
    ),
    ("paragraph", 0, "The recruiting budget is 45,000 dollars, pending sign-off from finance."),
    ("heading", 2, "Open questions"),
    (
        "paragraph",
        0,
        "It is unclear whether remote candidates can be considered before the policy review in August.",
    ),
    ("paragraph", 0, "Priya Nair coordinates interviews; Omar Haddad approves offers."),
]
QUALIFIED_HELDOUT = [
    ("heading", 1, "Facilities notes"),
    ("paragraph", 0, "The new office is expected to open in early March."),
    ("paragraph", 0, "Deliveries will not resume until the strike ends, which may take two weeks."),
    ("paragraph", 0, "The grant, if approved, would cover about 60 percent of the cost."),
    ("paragraph", 0, "According to the safety officer, the east exit is closed for repairs."),
]
ROLLOUT = [
    ("heading", 1, "Rollout plan"),
    ("paragraph", 0, "Training for all staff starts on 3 April."),
    ("paragraph", 0, "A shortage of trainers could delay the rollout by several weeks."),
    ("heading", 2, "Costs"),
    ("paragraph", 0, "The training budget is 18,000 dollars."),
]
DOCUMENTS = {
    "atlas": ATLAS,
    "qualified-dev": QUALIFIED_DEV,
    "launch-plan": corpus.DOCUMENT_ITEMS,
    "hiring": HIRING,
    "qualified-heldout": QUALIFIED_HELDOUT,
    "rollout": ROLLOUT,
}


def document_text(name: str) -> str:
    return "\n".join(t for _k, _l, t in DOCUMENTS[name])


# ---- the items ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QItem:
    id: str
    split: str  # dev | heldout
    finding: str  # F1 .. F7, the finding it tests
    kind: str  # answer | edit | draft
    instruction: str
    sources: tuple[str, ...] = ()
    document: str | None = None  # a key of DOCUMENTS
    include_document: bool = False  # answer items: is the open document switched on
    blocks: tuple[int, ...] = ()  # edit items: indexes of the selected blocks
    insert_at: str | None = None
    task_hint: str = "ask"
    concepts: tuple[Concept, ...] = ()  # the rubric: points a good output carries
    min_concepts: int = 0  # how many of them are enough (the pre-registered threshold)
    # per selected block: the facts its rewrite must keep (concepts), keyed by block index
    block_facts: dict[int, tuple[Concept, ...]] = field(default_factory=dict)
    expect_notice: str | None = None  # the context notice the request must carry
    material: tuple[
        str, ...
    ] = ()  # source or document names whose text the output may draw figures from
    expect_sources_cited: tuple[str, ...] = ()
    forbid_named_third_gate: bool = False
    forbid_invented_risks: bool = False
    expect_derived_citations: bool = False
    note: str = ""


def C(id: str, *forms: str) -> Concept:
    return Concept(id, tuple(forms))


ATLAS_RUBRIC = (
    C("what", r"order system", r"cloud", r"migrat"),
    C("deadline", r"30 june", r"june 30", r"end of june"),
    C("budget", r"120,?000"),
    C("risk", r"payment gateway", r"staging"),
    C("owners", r"maria", r"keller", r"tom", r"vogel", r"cutover", r"rollback"),
)
HIRING_RUBRIC = (
    C("what", r"support", r"agents?", r"hir(e|ing)"),
    C("deadline", r"15 september", r"september 15", r"mid[- ]september"),
    C("budget", r"45,?000"),
    C("pending", r"pending", r"sign-?off", r"finance"),
    C("open", r"remote", r"policy review", r"august", r"unclear"),
    C("owners", r"priya", r"nair", r"omar", r"haddad"),
)
RELEASE_DRAFT_RUBRIC = (
    C("gates", r"three gates", r"3 gates", r"gates"),
    C("build", r"compile", r"build"),
    C("tests", r"unit tests?"),
    C("deploy", r"restart", r"deploy"),
    C("owners", r"alice", r"bob"),
)
ONBOARDING_DRAFT_RUBRIC = (
    C("orientation", r"orientation", r"meet the team", r"tour"),
    C("payroll", r"payroll", r"dana"),
    C("equipment", r"equipment", r"laptops?", r"badges?", r"it desk", r"sam"),
    C("welcome", r"welcome", r"dear", r"hello", r"hi "),
)
ONBOARDING_SUMMARY_RUBRIC = (
    C("orientation", r"orientation", r"meet the team", r"tour"),
    C("payroll", r"payroll", r"dana"),
    C("equipment", r"equipment", r"laptops?", r"badges?", r"it desk", r"sam"),
)

DEV: tuple[QItem, ...] = (
    QItem(
        "closing-summary-atlas",
        "dev",
        "F1",
        "edit",
        "Add a closing summary paragraph.",
        document="atlas",
        insert_at="end",
        concepts=ATLAS_RUBRIC,
        min_concepts=4,
        material=("atlas",),
        note="P7 reproduced on a document with several distinct facts",
    ),
    QItem(
        "rewrite-qualified-dev",
        "dev",
        "F5",
        "edit",
        "Tighten these sentences without changing what they say.",
        document="qualified-dev",
        blocks=(1, 2, 3, 4),
        block_facts={
            1: (
                C("scheduled", r"scheduled", r"planned", r"slated", r"set for"),
                C("friday", r"friday"),
                C("noon", r"noon", r"12:00", r"12 ?pm"),
            ),
            2: (
                C("hedge", r"could", r"may", r"might", r"possibl"),
                C("vendor", r"vendor"),
                C("schedule", r"schedule"),
            ),
            3: (
                C("estimated", r"estimated", r"approximately", r"about", r"around", r"projected"),
                C("amount", r"40,?000", r"forty thousand"),
                C(
                    "not-approved",
                    r"not (yet )?approved",
                    r"unapproved",
                    r"awaiting approval",
                    r"pending approval",
                ),
            ),
            4: (
                C("conditional", r"\bif\b", r"provided", r"once", r"assuming", r"depending"),
                C("june", r"june"),
                C("queue", r"queue"),
            ),
        },
        material=("qualified-dev",),
        note="P5/P1: modality, status of the work, an estimate, an approval, a condition",
    ),
    QItem(
        "compare-depots-release",
        "dev",
        "F6",
        "answer",
        "Compare how the depot report and the release process assign responsibilities.",
        sources=("depots.pdf", "release.md"),
        task_hint="compare",
        concepts=(C("depots", r"depot"), C("release", r"release", r"gate", r"alice", r"bob")),
        min_concepts=2,
        expect_sources_cited=("depots.pdf", "release.md"),
    ),
    QItem(
        "draft-release-checklist",
        "dev",
        "F6",
        "draft",
        "Draft a short checklist for a new release owner from the release notes.",
        sources=("release.md",),
        concepts=RELEASE_DRAFT_RUBRIC,
        min_concepts=4,
        material=("release.md",),
        forbid_named_third_gate=True,
    ),
    QItem(
        "diagram-release-answer",
        "dev",
        "F4",
        "answer",
        "Draw a flowchart of the build and deploy steps.",
        sources=("release.md",),
        task_hint="auto",
        material=("release.md",),
        forbid_named_third_gate=True,
        note="A6/A7: Build then Deploy is not stated by the source",
    ),
    QItem(
        "diagram-release-edit",
        "dev",
        "F4",
        "edit",
        "Draw a flowchart of the build and deploy steps.",
        sources=("release.md",),
        document="launch-plan",
        blocks=(2,),
        material=("release.md",),
        forbid_named_third_gate=True,
        note="P4/P8",
    ),
    QItem(
        "review-launch-risks",
        "dev",
        "F7",
        "answer",
        "Review this document and list its risks.",
        document="launch-plan",
        include_document=True,
        task_hint="auto",
        concepts=(C("vendor-risk", r"vendor", r"delay"),),
        min_concepts=1,
        forbid_invented_risks=True,
        note="A5: 'could slip the schedule' is a stated effect",
    ),
    QItem(
        "missing-context-review",
        "dev",
        "F2",
        "answer",
        "Review this document and list its risks.",
        sources=("release.md",),
        include_document=False,
        task_hint="auto",
        expect_notice="document_not_included",
        note="the words ask for the open document; the request did not include it",
    ),
)

HELDOUT: tuple[QItem, ...] = (
    QItem(
        "closing-summary-hiring",
        "heldout",
        "F1",
        "edit",
        "Add a closing summary paragraph.",
        document="hiring",
        insert_at="end",
        concepts=HIRING_RUBRIC,
        min_concepts=4,
        material=("hiring",),
    ),
    QItem(
        "rewrite-qualified-heldout",
        "heldout",
        "F5",
        "edit",
        "Make these sentences shorter and clearer.",
        document="qualified-heldout",
        blocks=(1, 2, 3, 4),
        block_facts={
            1: (
                C("expected", r"expected", r"planned", r"scheduled", r"due", r"set to"),
                C("open", r"open"),
                C("march", r"march"),
                C("early", r"early"),
            ),
            2: (
                C(
                    "not-resume",
                    r"will not resume",
                    r"won't resume",
                    r"not resume",
                    r"no deliver",
                    r"suspended",
                    r"halted",
                ),
                C("until", r"until", r"before", r"once"),
                C("strike", r"strike"),
                C("hedge", r"may", r"could", r"might", r"possibl", r"up to"),
                C("two-weeks", r"two weeks", r"2 weeks", r"14 days"),
            ),
            3: (
                C("conditional", r"\bif\b", r"should", r"when", r"provided"),
                C("approved", r"approved", r"approval"),
                C("sixty", r"60", r"sixty"),
                C("about", r"about", r"approximately", r"around", r"roughly", r"~"),
            ),
            4: (
                C(
                    "attribution",
                    r"according to",
                    r"safety officer",
                    r"per the",
                    r"as per",
                    r"says the",
                    r"reports the",
                ),
                C("east-exit", r"east exit"),
                C("closed", r"closed", r"closure", r"shut"),
                C("repairs", r"repair"),
            ),
        },
        material=("qualified-heldout",),
    ),
    QItem(
        "compare-handbook-scan",
        "heldout",
        "F6",
        "answer",
        "Compare what the handbook and the scanned report say about operations and deliveries.",
        sources=("handbook.docx", "march-scan.pdf"),
        task_hint="compare",
        concepts=(
            C("handbook", r"handbook", r"pipeline", r"deploy", r"build"),
            C("scan", r"orders?", r"deliver", r"driver", r"warehouse", r"march"),
        ),
        min_concepts=2,
        expect_sources_cited=("handbook.docx", "march-scan.pdf"),
    ),
    QItem(
        "draft-onboarding-welcome",
        "heldout",
        "F6",
        "draft",
        "Draft a short welcome message for new hires from the onboarding notes.",
        sources=("onboarding.md",),
        concepts=ONBOARDING_DRAFT_RUBRIC,
        min_concepts=3,
        material=("onboarding.md",),
    ),
    QItem(
        "diagram-onboarding-answer",
        "heldout",
        "F4",
        "answer",
        "Draw a diagram of the onboarding steps.",
        sources=("onboarding.md",),
        task_hint="auto",
        material=("onboarding.md",),
        note="the three items are named; their order is not",
    ),
    QItem(
        "diagram-onboarding-edit",
        "heldout",
        "F4",
        "edit",
        "Draw a diagram of the onboarding steps.",
        sources=("onboarding.md",),
        document="rollout",
        insert_at="end",
        material=("onboarding.md",),
    ),
    QItem(
        "summarize-onboarding",
        "heldout",
        "F3",
        "answer",
        "Summarize the onboarding notes.",
        sources=("onboarding.md",),
        task_hint="summarize",
        concepts=ONBOARDING_SUMMARY_RUBRIC,
        min_concepts=3,
        material=("onboarding.md",),
        expect_derived_citations=True,
    ),
    QItem(
        "review-rollout-risks",
        "heldout",
        "F7",
        "answer",
        "Review this document and list its risks.",
        document="rollout",
        include_document=True,
        task_hint="auto",
        concepts=(C("trainer-risk", r"trainer", r"shortage"),),
        min_concepts=1,
        forbid_invented_risks=True,
    ),
    QItem(
        "missing-context-onboarding",
        "heldout",
        "F2",
        "answer",
        "Review this document and list its risks.",
        sources=("onboarding.md",),
        include_document=False,
        task_hint="auto",
        expect_notice="document_not_included",
    ),
)

ITEMS: tuple[QItem, ...] = DEV + HELDOUT


def split(name: str) -> tuple[QItem, ...]:
    return {"dev": DEV, "heldout": HELDOUT, "all": ITEMS}[name]


# ---- checks that are specific to this dataset's findings -------------------------------------------------------------

# a third gate named: "the third gate is X" with nothing that says it is unspecified within the next few words
_THIRD_GATE = re.compile(
    r"\b(third|3rd) gate\b(?![^.\n]{0,70}\b(not|unspecified|unnamed|unknown|missing|undefined|isn't|no |n't|yet|clear|described|stated|named|identified|detail|specif))",
    re.IGNORECASE,
)


def names_third_gate(text: str) -> bool:
    """Does the text give the third gate a name or a description the source does not give? A heuristic on the wording: it fires when
    "third gate" is not followed, in the same sentence, by a word that says it is unspecified. A flag to read, never a verdict."""
    return bool(_THIRD_GATE.search(text))


_EFFECT_MISSING = re.compile(
    r"\b(does not|doesn't|do not|no|not|without|lacks?|missing)\b[^.;\n]{0,50}\b(impact|effects?|consequences?)\b",
    re.IGNORECASE,
)
_QUANTIFIER = re.compile(
    r"\b(quantif\w*|size|magnitude|severity|how (much|long|many)|duration|amount|cost|extent|measur\w*|specific|exact|degree|numeric)\b",
    re.IGNORECASE,
)


def claims_effect_missing(text: str) -> bool:
    """Does the text say an EFFECT or impact is missing without saying what kind (a size, a likelihood, a cost)? In the review-sheet
    example the document says the delay "could slip the schedule": that is an effect, so "does not provide the impact" is wrong,
    while "does not quantify the impact" or "does not give the likelihood" is right. A heuristic on the wording."""
    for sentence in re.split(r"(?<=[.;])\s+|\n", text):
        if _EFFECT_MISSING.search(sentence) and not _QUANTIFIER.search(sentence):
            return True
    return False


_GENERIC_RISKS = re.compile(
    r"\b(budget (overrun|risk|shortfall)|cost overrun|security (breach|risk)|legal|regulator\w*|compliance|scope creep|staff(ing)? (shortage|turnover)|"
    r"data loss|cyber\w*|reputation\w*|vendor lock|key[- ]person|supply chain)\b",
    re.IGNORECASE,
)


def invented_risks(text: str) -> list[str]:
    """Generic risks a reviewer might add that no document of this dataset states (budget overrun, security, legal ...). A heuristic list:
    it names what to look at, and a hit inside a clearly labelled 'Beyond the document:' paragraph is allowed."""
    hits = []
    for para in re.split(r"\n+", text):
        if para.lower().startswith("beyond the document"):
            continue
        hits += [m.group(0) for m in _GENERIC_RISKS.finditer(para)]
    return hits


def dataset_fingerprint() -> str:
    blob = {
        "version": DATASET_VERSION,
        "items": [
            {
                "id": i.id,
                "split": i.split,
                "finding": i.finding,
                "kind": i.kind,
                "instruction": i.instruction,
                "sources": i.sources,
                "document": i.document,
                "include_document": i.include_document,
                "blocks": i.blocks,
                "insert_at": i.insert_at,
                "task_hint": i.task_hint,
                "concepts": [(c.id, c.any_of) for c in i.concepts],
                "min_concepts": i.min_concepts,
                "block_facts": {k: [(c.id, c.any_of) for c in v] for k, v in i.block_facts.items()},
                "expect_notice": i.expect_notice,
                "expect_sources_cited": i.expect_sources_cited,
            }
            for i in ITEMS
        ],
        "documents": {k: [list(x) for x in v] for k, v in DOCUMENTS.items()},
        "sources": {k: hashlib.sha256(v).hexdigest() for k, v in EXTRA_SOURCES.items()},
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()


def criteria() -> dict[str, Any]:
    """The acceptance criteria, as recorded in a run's manifest (the docstring above is the full statement)."""
    return {
        "declared": "before the first quality run; see the module docstring of tests/eval/quality.py",
        "guards": {
            "G1": "proposal schema gate >= 90% with at most one repair",
            "G2": "answer final route correct on >= 12 of 13, no classifier failure",
            "G3": "no item that published at baseline fails to publish",
        },
        "findings": {
            "F1": "closing summary: >= 4 rubric concepts, no unsupported figure, no placeholder (every item)",
            "F2": "missing context: published with the document_not_included notice, no document text sent (every item)",
            "F3": "derived citations: 100% open to verified original text, none incomplete",
            "F4": "diagrams that draw an order disclose it as assumed; no invented step or stage",
            "F5": "rewrites keep the facts of their rubric; the app flag marks >= 80% of rewrites that lost a qualification",
            "F6": "comparisons cite and mention both sources; drafts carry >= 4 rubric concepts and name no third gate",
            "F7": "reviews do not say a stated effect is missing and name no risk the document does not state",
        },
        "splits": "dev and held-out are reported separately; only held-out shows generalization",
    }
