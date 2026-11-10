"""The gold set for the phase 2 evaluation gate: edit proposals and drafts.

Each item names the target, the files it may draw on, and (stub mode only) the scripted replies the stand-in
model gives: some are well-formed, some carry the typical defects of a real model (a wrong label, a wrong
range, a hostile diagram, text that is not JSON). In `real` mode the scripts are ignored: the configured model
answers and every number is a measurement of it.

The document every edit item works on is the same small launch plan the phase 1 harness uses:
    B1 Launch plan (heading)   B2 The launch is scheduled for Friday at noon.   B3 The budget is forty
    thousand dollars.   B4 Risks (heading)   B5 Vendor delays could slip the schedule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.analysis_helpers import draft_reply, edit_reply, new_block, op, para, run_

GOOD_DIAGRAM = "flowchart TD\n  A[Compile the sources] --> B[Run the tests]\n  B --> C[Deploy]"
HOSTILE_DIAGRAM = 'flowchart TD\n  A --> B\n  click A href "javascript:alert(1)"'


@dataclass(frozen=True)
class PGold:
    id: str
    kind: str  # edit | draft
    instruction: str
    sources: tuple[str, ...] = ()
    blocks: tuple[int, ...] = ()  # zero-based indexes of selected blocks
    range_: tuple[int, int, int] | None = None  # (block index, start, end) in UTF-16 units
    insert_at: str | None = None
    script: tuple[Any, ...] = field(default=(), repr=False)  # stub replies, in order
    expect: str = "published"  # published | failed (a defect the model never repairs)
    defect: str | None = None  # what the FIRST scripted reply gets wrong, for the report


def cite(*labels: str) -> tuple[str, ...]:
    return labels


GOLD: tuple[PGold, ...] = (
    PGold(
        "edit-rewrite",
        "edit",
        "Rewrite this paragraph more clearly.",
        blocks=(1,),
        script=(
            edit_reply(
                op(
                    "replace_block_text",
                    block="B2",
                    new_inline=[run_("The launch is planned for Friday at 12:00.")],
                    reason="Clearer time.",
                )
            ),
        ),
    ),
    PGold(
        "edit-range",
        "edit",
        "Change only the word Friday to Thursday.",
        range_=(1, 28, 34),
        script=(
            edit_reply(
                op("replace_range", block="B2", start=28, end=34, new_inline=[run_("Thursday")])
            ),
        ),
    ),
    PGold(
        "edit-insert-from-release",
        "edit",
        "Add a build and deploy section using the release notes.",
        sources=("release.md",),
        blocks=(2,),
        script=(
            edit_reply(
                op(
                    "insert_blocks_after",
                    anchor="B3",
                    reason="From the release notes.",
                    refs=cite("P1"),
                    blocks=[
                        new_block("heading", level=2, inline=[run_("Build and deploy")]),
                        para(
                            "The build compiles the sources and the deploy restarts the workers.",
                            "P1",
                        ),
                    ],
                )
            ),
        ),
    ),
    PGold(
        "edit-insert-table",
        "edit",
        "Add a table of depot parcels from the depot report.",
        sources=("depots.pdf",),
        insert_at="end",
        script=(
            edit_reply(
                op(
                    "insert_blocks_after",
                    anchor="B5",
                    refs=cite("P1"),
                    blocks=[
                        new_block(
                            "table",
                            header_row=True,
                            rows=[["Depot", "Parcels"], ["Berlin", "4200"]],
                            citations=[{"ref": "P1", "quote": None}],
                        )
                    ],
                )
            ),
        ),
    ),
    PGold(
        "edit-delete",
        "edit",
        "Remove the budget paragraph.",
        blocks=(2,),
        script=(edit_reply(op("delete_blocks", block_refs=["B3"], reason="Not needed.")),),
    ),
    PGold(
        "edit-end-summary",
        "edit",
        "Add a closing summary paragraph.",
        insert_at="end",
        script=(
            edit_reply(
                op("insert_blocks_after", anchor="B5", blocks=[para("In short: launch on Friday.")])
            ),
        ),
    ),
    PGold(
        "edit-diagram",
        "edit",
        "Draw a flowchart of the build and deploy steps.",
        sources=("release.md",),
        blocks=(2,),
        script=(
            edit_reply(
                op(
                    "insert_blocks_after",
                    anchor="B3",
                    refs=cite("P1"),
                    blocks=[
                        new_block(
                            "diagram",
                            mermaid=GOOD_DIAGRAM,
                            citations=[{"ref": "P1", "quote": None}],
                        )
                    ],
                )
            ),
        ),
    ),
    PGold(
        "edit-two-operations",
        "edit",
        "Tighten the launch sentence and add a risk note after the risks paragraph.",
        blocks=(1, 4),
        script=(
            edit_reply(
                op("replace_block_text", block="B2", new_inline=[run_("Launch: Friday, noon.")]),
                op(
                    "insert_blocks_after",
                    anchor="B5",
                    blocks=[para("Mitigation: order spares early.")],
                ),
            ),
        ),
    ),
    # ---- defects a real model makes, and whether one repair fixes them -------------------------------------
    PGold(
        "edit-wrong-label-repaired",
        "edit",
        "Rewrite the budget line.",
        blocks=(2,),
        defect="a block label that was never shown",
        script=(
            edit_reply(op("replace_block_text", block="B9", new_inline=[run_("x")])),
            edit_reply(
                op("replace_block_text", block="B3", new_inline=[run_("The budget is 40k.")])
            ),
        ),
    ),
    PGold(
        "edit-wrong-range-repaired",
        "edit",
        "Change only the word Friday to Thursday.",
        range_=(1, 28, 34),
        defect="a range outside the declared one",
        script=(
            edit_reply(
                op("replace_range", block="B2", start=28, end=60, new_inline=[run_("Thursday")])
            ),
            edit_reply(
                op("replace_range", block="B2", start=28, end=34, new_inline=[run_("Thursday")])
            ),
        ),
    ),
    PGold(
        "edit-hostile-diagram-repaired",
        "edit",
        "Draw a flowchart of the deploy steps.",
        sources=("release.md",),
        blocks=(2,),
        defect="a diagram with a click handler",
        script=(
            edit_reply(
                op(
                    "insert_blocks_after",
                    anchor="B3",
                    refs=cite("P1"),
                    blocks=[
                        new_block(
                            "diagram",
                            mermaid=HOSTILE_DIAGRAM,
                            citations=[{"ref": "P1", "quote": None}],
                        )
                    ],
                )
            ),
            edit_reply(
                op(
                    "insert_blocks_after",
                    anchor="B3",
                    refs=cite("P1"),
                    blocks=[
                        new_block(
                            "diagram",
                            mermaid=GOOD_DIAGRAM,
                            citations=[{"ref": "P1", "quote": None}],
                        )
                    ],
                )
            ),
        ),
    ),
    PGold(
        "edit-not-json-repaired",
        "edit",
        "Rewrite this paragraph.",
        blocks=(1,),
        defect="a reply that is not JSON",
        script=(
            "Sure! Here is the rewritten paragraph: ...",
            edit_reply(
                op("replace_block_text", block="B2", new_inline=[run_("Launch on Friday at noon.")])
            ),
        ),
    ),
    PGold(
        "edit-never-valid",
        "edit",
        "Rewrite this paragraph.",
        blocks=(1,),
        expect="failed",
        defect="the same out-of-scope edit twice",
        script=(
            edit_reply(op("replace_block_text", block="B1", new_inline=[run_("x")])),
            edit_reply(op("replace_block_text", block="B1", new_inline=[run_("x")])),
        ),
    ),
    # ---- drafts -----------------------------------------------------------------------------------------------
    PGold(
        "draft-proposal",
        "draft",
        "Draft a short project proposal from the release notes.",
        sources=("release.md",),
        script=(
            draft_reply(
                "Release proposal",
                new_block("heading", level=1, inline=[run_("Release proposal")]),
                para(
                    "The release compiles the sources and deploys by restarting the workers.", "P1"
                ),
                new_block(
                    "bullet_list",
                    items=[{"text": "Compile", "depth": 0}, {"text": "Deploy", "depth": 0}],
                    citations=[{"ref": "P1", "quote": None}],
                ),
            ),
        ),
    ),
    PGold(
        "draft-with-diagram",
        "draft",
        "Draft a document with a flowchart of the build and deploy steps.",
        sources=("release.md",),
        script=(
            draft_reply(
                "Build and deploy",
                new_block("heading", level=1, inline=[run_("Build and deploy")]),
                new_block(
                    "diagram", mermaid=GOOD_DIAGRAM, citations=[{"ref": "P1", "quote": None}]
                ),
                para("Each step runs in order.", "P1"),
            ),
        ),
    ),
    PGold(
        "draft-two-sources",
        "draft",
        "Draft a comparison of how depots and releases are organised.",
        sources=("depots.pdf", "release.md"),
        defect="a first draft whose paragraphs cite nothing",
        script=(
            draft_reply(
                "Depots and releases",
                new_block("heading", level=1, inline=[run_("Depots and releases")]),
                para("Depots report monthly."),
                para("Releases follow steps."),
            ),
            draft_reply(
                "Depots and releases",
                new_block("heading", level=1, inline=[run_("Depots and releases")]),
                para("Depots report monthly.", "P1"),
                para("Releases follow steps.", "P2"),
            ),
        ),
    ),
)
