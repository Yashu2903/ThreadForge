# P7h · The review's P7 as it was in run 1 (repaired reply)

* **Item:** `edit-end-summary`
* **Run:** `run1-proposal` (HISTORICAL: the build that was reviewed; some evidence was not recorded then)
* **Code of that run:** unavailable
* **Request id:** `not recorded` (this run did not keep it)
* **Outcome:** published

## 1. The request

**Instruction, exactly as sent:**

> Add a closing summary paragraph.

* **Operation:** edit
* **Sources selected (names, immutable versions):**
  none selected
* **Open document:** none (no open document was read for this request)

## 2. What the request read (original material)

unavailable: this record has no ranked candidates (earlier run). The sources are in `../fixtures/`; the reader can open the cited passages below.

Fixture files for this item (synthetic):
* [`launch-plan.md`](../fixtures/launch-plan.md)

## 3. The output, complete

### Change 1: `insert_blocks_after`

*The model's reason:* Add the requested closing paragraph without introducing unsupported specifics.
*Cites:* nothing

**Before:**

(nothing)

**After:**

In summary, the preceding discussion brings together the key points presented above.



## 4. Citations and what they open to

This output cites nothing (no evidence label was used).

## 5. Automated results (mechanical; none is a judgement of truth, completeness or usefulness)

| Check | What it is | Result |
|---|---|---|
| Published | the request produced a pending proposal | True |
| Valid on the first reply / repaired | the first reply passed the shared document engine / one repair was used (codes of the first problems) | False / True (wrong_operation_count) |
| Target blocks and anchors resolve | the base hashes and anchors still match the captured document and a replay of the operations is valid | True |
| Citation validity | share of operations or blocks whose citations are valid (not a check of support) | None |
| Qualification review flags | the app's word-level review notes on rewrites (hedges, state of the work, negation, conditions, attribution, figures); a note, never a verdict | unavailable in this run |
| Latency (s) | end to end | 7.5 |

## 6. Agent assessment (my own reading, written by hand after the run; it is NOT a human review and it can be wrong)

* The repaired reply of run 1: a generic closing sentence, and the model's own summary says the document content was not provided. The first reply had the wrong number of operations. It passed every automated check, which is the point of the finding.
* Cause, as far as it can be established: for an insertion with no selected blocks the old prompt held no document text (shown by a test on the old behaviour, and reproduced in the baseline rerun on two other documents, where the model again said the content was not provided). What run 1 actually sent was not stored, so the cause is read from the code and the model's own words, not from the stored prompt. The repair call re-sent the same context as the first call, so repair did not lose anything the first call had.

## 7. Human review (leave blank until you assess it)

| Field | Your assessment |
|---|---|
| Factual support: is every statement supported by the original text shown above? (supported / partly / unsupported / not applicable) | |
| Completeness: does it cover what was asked? (complete / partial / missing key points) | |
| Usefulness: would you use it? (1 poor ... 5 very useful) | |
| Edit safety: would you apply it as is? (safe / needs changes / unsafe) and what would go wrong if applied | |
| Meaning preserved: does the new text keep every qualification of the old (planned vs done, may vs will, numbers, conditions, who said it)? | |
| Notes | |
