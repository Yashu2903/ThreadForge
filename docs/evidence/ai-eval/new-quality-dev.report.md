# Quality evaluation (REAL provider)

Dataset `quality-v1` (fingerprint 2831ad4f503e), split `dev`. Run: 2026-11-21 00:42 UTC. Items: 8. Criteria: tests/eval/quality.py (declared before the first run).

Three kinds of result, kept apart: **structural** (valid, rendered, cited), **semantic** (deterministic content checks against a written rubric), **finding** (this item against its finding's criterion). None is a human review or a judge.

## Split `dev`

| Finding | Items | Published | Finding met | Items |
|---|---|---|---|---|
| F1 | 1 | 1/1 | 1/1 | closing-summary-atlas (met) |
| F2 | 1 | 1/1 | 1/1 | missing-context-review (met) |
| F4 | 2 | 2/2 | 2/2 | diagram-release-answer (met), diagram-release-edit (met) |
| F5 | 1 | 1/1 | 1/1 | rewrite-qualified-dev (met) |
| F6 | 2 | 2/2 | 2/2 | compare-depots-release (met), draft-release-checklist (met) |
| F7 | 1 | 1/1 | 1/1 | review-launch-risks (met) |

F5 flag recall (rewritten sentences that lost a fact of their rubric and were marked by the app's review flag): 0/0 (nothing was lost, so nothing to flag)

## Per item

| Item | Split | Finding | Published | Finding met | Why (checks) |
|---|---|---|---|---|---|
| closing-summary-atlas | dev | F1 | yes | yes | concepts 5/5 (need 4), missing [] |
| rewrite-qualified-dev | dev | F5 | yes | yes | 4 sentences changed; facts lost: none; flagged by the app: n/a |
| compare-depots-release | dev | F6 | yes | yes | cites ['depots.pdf', 'release.md'] (need ['depots.pdf', 'release.md']); concepts 2/2 |
| draft-release-checklist | dev | F6 | yes | yes | cites ['release.md'] (need n/a); concepts 4/5 |
| diagram-release-answer | dev | F4 | yes | yes | diagram checks [{'draws_order': True, 'order_disclosed': True, 'possibly_invented_labels': []}] |
| diagram-release-edit | dev | F4 | yes | yes | diagram checks [{'draws_order': True, 'order_disclosed': True, 'possibly_invented_labels': []}] |
| review-launch-risks | dev | F7 | yes | yes | concepts 1/1; effect wrongly called missing: False; invented risks: [] |
| missing-context-review | dev | F2 | yes | yes | notices ['document_not_included']; document read: False |

Latency p50 / p95 per request (s): 7.80 / 14.70. Model calls per request (mean): 2.00. Input / output tokens per request (mean): 1750.50 / 662.12.

## Provider failures (run-wide usage ledger)

Failed provider calls: 0/18 (0%) of all recorded calls.

None.

## Cost

* Status: **unpriced**. Cost: **unknown**.
* Tokens (kept even when pricing is missing): generation 13842 in / 5297 out; embeddings 3155 in.
* A ledger or table cost of 0 means not priced, never free. not available: the providers' responses report tokens, not cost.
* Rates: none configured.

## How to read this

* One sample per item per run; a single item flipping is noise. Dev results show the fixes work on what they were built from; only held-out results can show they generalize.
* `concepts` is a recall check on named points, `figures` a check on numbers and times, `placeholder` a diagnostic for one known failure, `order disclosed` a check on what the output SAID. They cannot tell whether a statement is true or an output useful: that is read by hand (agent assessment) and by a human reviewer.
* Metric versions: required_phrase_coverage = phrase-v1, evidence_recall = evidence-v1.
