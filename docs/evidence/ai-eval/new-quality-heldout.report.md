# Quality evaluation (REAL provider)

Dataset `quality-v1` (fingerprint 2831ad4f503e), split `heldout`. Run: 2026-11-21 00:45 UTC. Items: 9. Criteria: tests/eval/quality.py (declared before the first run).

Three kinds of result, kept apart: **structural** (valid, rendered, cited), **semantic** (deterministic content checks against a written rubric), **finding** (this item against its finding's criterion). None is a human review or a judge.

## Split `heldout`

| Finding | Items | Published | Finding met | Items |
|---|---|---|---|---|
| F1 | 1 | 1/1 | 1/1 | closing-summary-hiring (met) |
| F2 | 1 | 1/1 | 1/1 | missing-context-onboarding (met) |
| F3 | 1 | 1/1 | 1/1 | summarize-onboarding (met) |
| F4 | 2 | 2/2 | 2/2 | diagram-onboarding-answer (met), diagram-onboarding-edit (met) |
| F5 | 1 | 1/1 | 1/1 | rewrite-qualified-heldout (met) |
| F6 | 2 | 2/2 | 2/2 | compare-handbook-scan (met), draft-onboarding-welcome (met) |
| F7 | 1 | 1/1 | 1/1 | review-rollout-risks (met) |

F5 flag recall (rewritten sentences that lost a fact of their rubric and were marked by the app's review flag): 0/0 (nothing was lost, so nothing to flag)

## Per item

| Item | Split | Finding | Published | Finding met | Why (checks) |
|---|---|---|---|---|---|
| closing-summary-hiring | heldout | F1 | yes | yes | concepts 6/6 (need 4), missing [] |
| rewrite-qualified-heldout | heldout | F5 | yes | yes | 3 sentences changed; facts lost: none; flagged by the app: n/a |
| compare-handbook-scan | heldout | F6 | yes | yes | cites ['handbook.docx', 'march-scan.pdf'] (need ['handbook.docx', 'march-scan.pdf']); concepts 2/2 |
| draft-onboarding-welcome | heldout | F6 | yes | yes | cites ['onboarding.md'] (need n/a); concepts 4/4 |
| diagram-onboarding-answer | heldout | F4 | yes | yes | diagram checks [{'draws_order': False, 'order_disclosed': True, 'possibly_invented_labels': []}] |
| diagram-onboarding-edit | heldout | F4 | yes | yes | diagram checks [{'draws_order': False, 'order_disclosed': True, 'possibly_invented_labels': []}] |
| summarize-onboarding | heldout | F3 | yes | yes | derived citations resolve: True; provenance incomplete: 0; concepts 3/3 (need 3) |
| review-rollout-risks | heldout | F7 | yes | yes | concepts 1/1; effect wrongly called missing: False; invented risks: [] |
| missing-context-onboarding | heldout | F2 | yes | yes | notices ['document_not_included']; document read: False |

Latency p50 / p95 per request (s): 6.69 / 11.39. Model calls per request (mean): 2.00. Input / output tokens per request (mean): 1625.44 / 586.33.

## Provider failures (run-wide usage ledger)

Failed provider calls: 0/23 (0%) of all recorded calls.

None.

## Cost

* Status: **unpriced**. Cost: **unknown**.
* Tokens (kept even when pricing is missing): generation 15075 in / 5380 out; embeddings 1326 in.
* A ledger or table cost of 0 means not priced, never free. not available: the providers' responses report tokens, not cost.
* Rates: none configured.

## How to read this

* One sample per item per run; a single item flipping is noise. Dev results show the fixes work on what they were built from; only held-out results can show they generalize.
* `concepts` is a recall check on named points, `figures` a check on numbers and times, `placeholder` a diagnostic for one known failure, `order disclosed` a check on what the output SAID. They cannot tell whether a statement is true or an output useful: that is read by hand (agent assessment) and by a human reviewer.
* Metric versions: required_phrase_coverage = phrase-v1, evidence_recall = evidence-v1.
