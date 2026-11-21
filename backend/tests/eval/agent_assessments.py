"""My own readings of the sampled outputs of the review material v2 (build_review_sheet_v2.py): AGENT ASSESSMENTS.

Written by hand, after reading each output in full, and shown in every item under a heading that says what they are. They are NOT a human
review and NOT automated checks, they can be wrong, and they are kept apart from both. Where the automated result and my reading differ, the
item shows both. The human fields stay empty: this file never fills them.

Each entry: what I checked, what I found, what I did not check.
"""

from __future__ import annotations

AGENT: dict[str, list[str]] = {
    "Q1": [
        "Checked against the document text (fixtures/atlas.md): the paragraph carries the move to the cloud by 30 June, the 120,000 euro budget approved by the steering group, "
        "the payment-gateway risk without a staging environment, and both plan owners. I found no statement that is not in the document and no new figure.",
        "It is two dense sentences, not a structured closing; whether that reads well is yours to judge. The earlier build could not have done this: it was never shown the document (see the baseline run's item for the same request).",
    ],
    "Q2": [
        "Held-out document (fixtures/hiring.md). The paragraph carries all six points and keeps the qualifications: the budget is 'pending finance sign-off', and it 'remains unclear' whether remote candidates "
        "can be considered before the August policy review. Both named people and their roles are right. I found nothing invented.",
        "'In summary, ...' opens the paragraph, which is how a closing summary usually starts; it is followed by content, which is the difference from the placeholder of the review sheet's P7.",
    ],
    "Q3": [
        "Four rewrites against the four sentences of fixtures/qualified-dev.md. 'Launch is scheduled for Friday at noon.' is the concise rewrite the review accepted. 'estimated at $40,000 and is not yet approved' keeps the estimate, "
        "the amount in another notation, and the pending approval. 'Support will move ... if migration finishes by June' keeps the condition and the month.",
        "'could push back the schedule' for 'could slip the schedule': same hedge, same meaning; 'push back' is not clearer than 'slip' and is six characters longer, so I would not call that edit an improvement. "
        "The app raised no flag; I agree there was nothing lost. I did not check whether the tightened sentences are 'clearer'; that is yours.",
    ],
    "Q4": [
        "Held-out sentences (fixtures/qualified-heldout.md). The model left the first sentence alone and said so ('already concise'), which is a legitimate answer to 'make shorter and clearer'. The other three keep their qualifications: "
        "'won't resume until the strike ends, which may take two weeks' (negation, condition, hedge, duration), 'If approved, the grant would cover about 60 percent' (condition, 'about', the figure), "
        "'The safety officer says the east exit is closed for repairs' (attribution kept; 'says' for 'according to' is the same attribution).",
        "The app flagged the first as 'will is gone: the text no longer says the work is planned'. That flag was WRONG: 'won't' is 'will not'. It was a defect in the check (contractions were not read), found by this run and fixed afterwards "
        "(tests/test_qualifiers.py::test_a_negative_contraction...). The run itself was made before the fix, so the flag you see here is the false positive.",
    ],
    "Q5": [
        "Look at fixtures/onboarding.md: three items (Orientation, Payroll, Equipment) and no order. The diagram groups them in three boxes with no arrows, and its title says 'no sequence specified', "
        "which also appears in the chat text. Every box text is in the source (Dana, Sam and the IT desk are the source's own words). Nothing is invented.",
        "This is the behaviour the contract asks for when order is not stated: group, do not chain. I did not check how the diagram looks when rendered; the automated check says only that the Mermaid parses.",
    ],
    "Q6": [
        "The same source as an edit proposal: a Mermaid mindmap with 'Onboarding' at the centre and the three items as branches, no ordering, and the model's own summary says it groups the items 'without implying a sequence, since no order is specified'. "
        "Its four citations point at the source sections.",
        "A mindmap is a different diagram type from the flowchart of Q5; both are allowed by the contract. Nothing needed an assumption note, so none was added, and none is shown in the preview.",
    ],
    "Q7": [
        "Each bullet matches the source (orientation: meet the team and tour the warehouse; Dana handles payroll; IT desk issues laptops and badges, Sam approves requests). Open each citation: they are section summaries "
        "written by the model, labelled as derived, and each opens to the original section text with a verified stored-text check.",
        "The summary is short and complete for a three-section file. Whether a section summary 'supports' its bullet is something I read by comparing the two on screen; the check in the table only says the lineage opens.",
    ],
    "Q8": [
        "The document (fixtures/launch-plan.md) states one risk, 'Vendor delays could slip the schedule.' The answer lists that risk and says the document 'states both the risk and its possible effect', then names what is really missing: how likely, who owns it, what mitigation. "
        "That is the correction the review asked for: the earlier answer said impact was missing although slippage of the schedule is an effect.",
        "It does not mention the budget paragraph, which states a fact and no risk, and I think that is right. No risk is invented.",
    ],
    "Q9": [
        "The words ask for the open document, which this request did not include. The chat message begins with the notice and how to include the document, and the request read no document (checked in the manifest). "
        "The answer is then about the selected file, release.md.",
        "Some bullets are the model's own speculation about release.md rather than statements in it ('defects not caught by those checks may go undetected', 'failure handling or recovery' is not described) and each still cites a passage that does not say so. "
        "Because the request ran as a plain question, the 'Beyond the document:' convention of document reviews did not apply. That is a remaining weakness, not a defect this change introduced.",
    ],
    "Q10": [
        "Both selected files are cited (handbook.docx and march-scan.pdf) and both are described. The handbook paragraph (deployment pipeline, Alice and Bob, 'build the release candidate') and the scanned-report paragraph "
        "(12,000 orders in March, driver shortage, hiring, satisfaction, maintenance, training) match the sources; the OCR caveat is stated.",
        "The comparison concludes the files cover different subjects and says neither describes the other's topic; I think that is the honest comparison of these two files.",
    ],
    "Q11": [
        "A short welcome paragraph and a bullet list with the four facts of the onboarding notes, and a Sources section the server added from the citations. It does what 'a short welcome message' asks, in a plain way; the tone is yours to judge.",
    ],
}
