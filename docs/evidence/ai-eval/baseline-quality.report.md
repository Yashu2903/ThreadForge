# Quality evaluation (REAL provider)

Dataset `quality-v1` (fingerprint 2831ad4f503e), split `all`. Run: 2026-11-21 00:48 UTC. Items: 17. Criteria: tests/eval/quality.py (declared before the first run).

Three kinds of result, kept apart: **structural** (valid, rendered, cited), **semantic** (deterministic content checks against a written rubric), **finding** (this item against its finding's criterion). None is a human review or a judge.

## Split `dev`

| Finding | Items | Published | Finding met | Items |
|---|---|---|---|---|
| F1 | 1 | 1/1 | 0/1 | closing-summary-atlas (NOT met) |
| F2 | 1 | 1/1 | 0/1 | missing-context-review (NOT met) |
| F4 | 2 | 2/2 | 0/2 | diagram-release-answer (NOT met), diagram-release-edit (NOT met) |
| F5 | 1 | 1/1 | 1/1 | rewrite-qualified-dev (met) |
| F6 | 2 | 2/2 | 2/2 | compare-depots-release (met), draft-release-checklist (met) |
| F7 | 1 | 1/1 | 1/1 | review-launch-risks (met) |

F5 flag recall (rewritten sentences that lost a fact of their rubric and were marked by the app's review flag): 0/0 (nothing was lost, so nothing to flag)

## Split `heldout`

| Finding | Items | Published | Finding met | Items |
|---|---|---|---|---|
| F1 | 1 | 1/1 | 0/1 | closing-summary-hiring (NOT met) |
| F2 | 1 | 1/1 | 0/1 | missing-context-onboarding (NOT met) |
| F3 | 1 | 1/1 | 0/1 | summarize-onboarding (NOT met) |
| F4 | 2 | 2/2 | 1/2 | diagram-onboarding-answer (NOT met), diagram-onboarding-edit (met) |
| F5 | 1 | 1/1 | 0/1 | rewrite-qualified-heldout (NOT met) |
| F6 | 2 | 2/2 | 2/2 | compare-handbook-scan (met), draft-onboarding-welcome (met) |
| F7 | 1 | 1/1 | 1/1 | review-rollout-risks (met) |

F5 flag recall (rewritten sentences that lost a fact of their rubric and were marked by the app's review flag): 0/1

## Per item

| Item | Split | Finding | Published | Finding met | Why (checks) |
|---|---|---|---|---|---|
| closing-summary-atlas | dev | F1 | yes | NO | concepts 0/5 (need 4), missing ['what', 'deadline', 'budget', 'risk', 'owners'] |
| rewrite-qualified-dev | dev | F5 | yes | yes | 3 sentences changed; facts lost: none; flagged by the app: n/a |
| compare-depots-release | dev | F6 | yes | yes | cites ['depots.pdf', 'release.md'] (need ['depots.pdf', 'release.md']); concepts 2/2 |
| draft-release-checklist | dev | F6 | yes | yes | cites ['release.md'] (need n/a); concepts 5/5 |
| diagram-release-answer | dev | F4 | yes | NO | diagram checks [{'draws_order': True, 'order_disclosed': False, 'possibly_invented_labels': []}] |
| diagram-release-edit | dev | F4 | yes | NO | diagram checks [{'draws_order': True, 'order_disclosed': False, 'possibly_invented_labels': []}] |
| review-launch-risks | dev | F7 | yes | yes | concepts 1/1; effect wrongly called missing: False; invented risks: [] |
| missing-context-review | dev | F2 | yes | NO | notices []; document read: False |
| closing-summary-hiring | heldout | F1 | yes | NO | concepts 0/6 (need 4), missing ['what', 'deadline', 'budget', 'pending', 'open', 'owners'] |
| rewrite-qualified-heldout | heldout | F5 | yes | NO | 3 sentences changed; facts lost: {'2': ['not-resume', 'until']}; flagged by the app: {'2': False} |
| compare-handbook-scan | heldout | F6 | yes | yes | cites ['handbook.docx', 'march-scan.pdf'] (need ['handbook.docx', 'march-scan.pdf']); concepts 2/2 |
| draft-onboarding-welcome | heldout | F6 | yes | yes | cites ['onboarding.md'] (need n/a); concepts 4/4 |
| diagram-onboarding-answer | heldout | F4 | yes | NO | diagram checks [{'draws_order': True, 'order_disclosed': True, 'possibly_invented_labels': ['Sam approves equipment requests']}] |
| diagram-onboarding-edit | heldout | F4 | yes | yes | diagram checks [{'draws_order': True, 'order_disclosed': True, 'possibly_invented_labels': []}] |
| summarize-onboarding | heldout | F3 | yes | NO | derived citations resolve: None; provenance incomplete: None; concepts 3/3 (need 3) |
| review-rollout-risks | heldout | F7 | yes | yes | concepts 1/1; effect wrongly called missing: False; invented risks: [] |
| missing-context-onboarding | heldout | F2 | yes | NO | notices []; document read: False |

Latency p50 / p95 per request (s): 7.62 / 9.22. Model calls per request (mean): 2.12. Input / output tokens per request (mean): 1437.88 / 494.88.

## Provider failures (run-wide usage ledger)

Failed provider calls: 0/43 (0%) of all recorded calls.

None.

## Cost

* Status: **unpriced**. Cost: **unknown**.
* Tokens (kept even when pricing is missing): generation 24728 in / 8516 out; embeddings 4481 in.
* A ledger or table cost of 0 means not priced, never free. not available: the providers' responses report tokens, not cost.
* Rates: none configured.

## How to read this

* One sample per item per run; a single item flipping is noise. Dev results show the fixes work on what they were built from; only held-out results can show they generalize.
* `concepts` is a recall check on named points, `figures` a check on numbers and times, `placeholder` a diagnostic for one known failure, `order disclosed` a check on what the output SAID. They cannot tell whether a statement is true or an output useful: that is read by hand (agent assessment) and by a human reviewer.
* Metric versions: required_phrase_coverage = phrase-v1, evidence_recall = evidence-v1.
