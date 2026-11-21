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
    "A1": [
        "Open the citation: the original passage is page 1 of depots.pdf and says 4200 parcels. The answer writes 4,200, the same number. One claim, one citation, nothing I could not find in the original text.",
    ],
    "A2": [
        "I looked at fixtures/architecture-figure.png. It has four boxes (Client, API, Worker, Database) left to right, joined by plain lines with NO arrowheads. The answer says exactly that, including that the lines do not show arrowheads. "
        "The citation opens to a machine-generated figure description, labelled as such, and the figure image itself.",
        "Correction to the first sheet: its reading of this item said the answer 'matches the figure' and its diagram answer drew arrows (Client --> API). I had not checked the image for arrowheads then; it has none, so a direction was an inference.",
    ],
    "A3": [
        "Each of the three statements traces to one of the three section summaries (S1-S3). Open each citation: it is labelled DERIVED (a summary the model wrote) and shows the original section text with a verified stored-text check. "
        "The summaries say what the originals say (three gates; Build compiles and runs tests; Alice owns Build, Bob owns Deploy; workers restart one at a time). The gap 'does not name the third gate' is accurate.",
        "What a derived citation shows is lineage, not support: I compared each summary with its original by eye and found no addition, but that comparison is mine, not a check the system makes.",
    ],
    "A4": [
        "Both files are covered. The depot paragraph (morning checks, route collection and scanning, weekly delay summaries) and its local examples (Berlin six more staff, Madrid a third belt, Oslo paused night shifts, Paris a second dock) are all in depots.pdf; "
        "the release paragraph matches release.md. The gaps (third gate unspecified; the order of stages not stated) are accurate.",
        "Citation granularity is coarse: the depot paragraph cites eight passages together, so to check one example you open several. This answer is less thin than the one in the first sheet, which summarised only the repeated filler.",
    ],
    "A5": [
        "The document states one risk and its effect. The answer lists it, says the stated effect is that the schedule could slip, and names what is really missing (how likely, what mitigation). That is the correction the review asked for: the first sheet's answer said impact was missing.",
        "'Beyond the document: ...' is a labelled suggestion and it cites nothing new. No risk is invented. In the baseline rerun of this request the wording defect did not reappear (see the comparison), so this item shows the new wording, not proof that the prompt change caused it.",
    ],
    "A6": [
        "The diagram chains Build, its two tasks, Deploy and the restart step with arrows. The release notes list Build before Deploy but never say one follows the other, and they do not say the two tasks run in that order. The answer says so in two assumptions, "
        "which the card shows next to the diagram and the chat message repeats. The third gate is mentioned as unspecified, not named.",
        "One oddity: the node 'Build - Alice' is followed by arrows to its own tasks, which draws a containment as a sequence. The assumptions disclose the ordering, not that choice. Rendering was checked by the Mermaid parser only; look at it.",
    ],
    "A7": [
        "The earlier run at default effort, kept as it was. It routed to summarize and cited section summaries, so the automated 'required phrase in cited text' check scored it 0 although its diagram, third-gate note and order disclosure were sound. "
        "The old retrieval 'recall' of 0 for this item came from the same cause: the summarize route retrieves no passages. The new evidence measure reports coverage of the sections read for summarize instead.",
    ],
    "A8": [
        "Undirected: Client --- API --- Worker --- Database. It matches the figure, which has lines and no arrowheads (see fixtures/architecture-figure.png). The earlier build drew arrows here; this answer does not claim a direction the image does not show.",
    ],
    "P1": [
        "'The launch is scheduled for noon on Friday.' Same meaning, same length as the original (the first sheet's output was longer). 'Scheduled' is kept and so are the day and the time. I would not call it clearer; it is a reordering. The app raised no flag, and I found nothing lost.",
        "The model's own summary says it 'rephrased for clarity'. That is the model's claim; the preview labels it 'Why' (the model's reason), and nothing in the system verifies it.",
    ],
    "P2": [
        "Inserts a heading, the three-gates statement (with the third gate marked as not named), and the Build and Deploy owners with their tasks, as the release notes say. Where it lands depends on the anchor (after the budget paragraph): check the placement in the preview. "
        "Open the citations: each passage is the original text, and the figures and names match.",
    ],
    "P3": [
        "Deletes exactly the budget paragraph that was asked for. The complete target before and after is shown. A deletion is destructive until applied, so it needs your explicit approval.",
    ],
    "P4": [
        "The diagram is the same chain as A6 and the preview states what it assumes (the order of the tasks and of Build before Deploy), and an italic note repeating it follows the diagram in the inserted blocks, so the caveat travels into the document if applied. "
        "The three citations open to the original release.md passages.",
    ],
    "P5": [
        "Both operations stay inside the launch plan. The sentence now keeps 'scheduled' ('Launch is scheduled for Friday at noon.'), the concise rewrite the review described as acceptable, and the risk note restates the existing risk. "
        "The dropped 'scheduled' of the first review did not recur in this sample. The check that would have caught it did: applied to the stored first output it flags 'scheduled is gone' (evidence: historical-rewrite-flags.json).",
    ],
    "P6": [
        "The complete draft is in section 3. It stays within the release notes: three gates, Build and Deploy with their owners and tasks, the third gate described as mentioned but not described, a next step, and a Sources section the server built from the citations. "
        "'A proposal to formalize the release process into a runbook' is a loose reading of 'a short project proposal from the release notes'; whether it is what you wanted is yours to judge.",
    ],
    "P7": [
        "The same request that produced a placeholder in run 1, now run on the build that sends the document as context. The inserted paragraph states the launch timing, the budget and the schedule risk with its 'could', all from the document. No placeholder; no figure that is not in the document.",
        "It is one sentence listing three facts and the document has five blocks, so this is not a stress test of a long document; the Atlas and hiring items (Q1, Q2) are.",
    ],
    "P7h": [
        "The repaired reply of run 1: a generic closing sentence, and the model's own summary says the document content was not provided. The first reply had the wrong number of operations. It passed every automated check, which is the point of the finding.",
        "Cause, as far as it can be established: for an insertion with no selected blocks the old prompt held no document text (shown by a test on the old behaviour, and reproduced in the baseline rerun on two other documents, where the model again said the content was not provided). "
        "What run 1 actually sent was not stored, so the cause is read from the code and the model's own words, not from the stored prompt. The repair call re-sent the same context as the first call, so repair did not lose anything the first call had.",
    ],
    "P8h": [
        "The repaired reply of run 2: plain Mermaid, a chain Build, its tasks, Deploy, with owners, matching the source's facts. The unsafe first reply (HTML in a diagram) never reached the preview. Like A6, it drew an order the source does not state and said nothing about it; the new build's P4 is the same request with the assumption disclosed.",
    ],
    "Q11": [
        "A short welcome paragraph and a bullet list with the four facts of the onboarding notes, and a Sources section the server added from the citations. It does what 'a short welcome message' asks, in a plain way; the tone is yours to judge.",
    ],
}
