# Evaluation results (REAL providers)

Run: 2026-11-21 00:55 UTC. Gold items: 13; published: 13. Sources: depots.pdf, march-scan.pdf, handbook.docx, release.md.

| Metric | Value | Basis |
|---|---|---|
| Requests that published an answer | 100% (13/13) | measured (real request pipeline) |
| required_phrase_coverage@1 (phrase-v1: mean share of required phrases found in the top-1 evidence passages; NOT retrieval recall) | 85% | measured |
| required_phrase_coverage@3 | 100% | same |
| required_phrase_coverage@5 | 100% | same |
| required_phrase_coverage over all evidence sent to the model | 100% | same |
| Items with every required phrase in the evidence | 100% | same |
| evidence_recall@1 (evidence-v1: share of gold evidence units covered by the top-ranked ORIGINAL passages; 10 ranked items with gold, 10/12 units pooled) | 90% | measured against evidence-gold (evidence-gold-v1) |
| evidence_recall@3 (evidence-v1: share of gold evidence units covered by the top-ranked ORIGINAL passages; 10 ranked items with gold, 12/12 units pooled) | 100% | measured against evidence-gold (evidence-gold-v1) |
| evidence_recall@5 (evidence-v1: share of gold evidence units covered by the top-ranked ORIGINAL passages; 10 ranked items with gold, 12/12 units pooled) | 100% | measured against evidence-gold (evidence-gold-v1) |
| evidence_recall@all (evidence-v1: share of gold evidence units covered by the top-ranked ORIGINAL passages; 10 ranked items with gold, 12/12 units pooled) | 100% | measured against evidence-gold (evidence-gold-v1) |
| evidence_coverage (evidence-v1: gold units covered by the sections or document READ, unranked; 2 items, 1/4 units pooled; NOT comparable with recall) | 50% | measured |
| Derived citations that open to verified original text | n/a | structure only: lineage, not support |
| OCR character accuracy (3 scanned pages) | 92% | measured (real Tesseract, with the vision fallback available) |
| Final route matched the expected route (fallback included) | 13/13 (100%) | measured; NOT a model classification, see Routing |
| Citations without an invalid reference or dropped quote | 100% | measured |
| Answers that cite the expected source(s) | 100% | measured |
| Cited text contains the required phrase | 100% | measured |
| Schema-valid answers without a repair call | 100% | measured |
| Diagram requests that published a diagram | 100% | measured |
| Diagrams that render (Mermaid parses) | 100% | measured |
| Diagrams that draw directed relations: say they are assumed (what the output SAID; not a check of truth) | 0/2 | measured |
| Figure questions that cite a figure | 100% | measured |
| Summaries/comparisons with full section coverage | 100% | measured (server-computed coverage) |
| Latency p50 / p95 per request (s) | 4.11 / 9.19 | measured, end to end |

## Per task type

| Task | Items | Published | Calls | Input tokens | Output tokens | Est. cost (USD) | Latency p50 (s) | Basis for tokens/cost |
|---|---|---|---|---|---|---|---|---|
| compare | 1 | 1 | 3.0 | 2647 | 737 | unknown (no rates configured) | 8.03 | provider-reported |
| diagram | 2 | 2 | 3.0 | 1310 | 238 | unknown (no rates configured) | 7.72 | provider-reported |
| figure | 1 | 1 | 3.0 | 1622 | 151 | unknown (no rates configured) | 6.30 | provider-reported |
| qa | 7 | 7 | 3.0 | 1714 | 102 | unknown (no rates configured) | 3.57 | provider-reported |
| review | 1 | 1 | 2.0 | 954 | 192 | unknown (no rates configured) | 4.11 | provider-reported |
| summarize | 1 | 1 | 3.0 | 1190 | 341 | unknown (no rates configured) | 6.26 | provider-reported |

(per-item means across the request's own calls; analysis of the sources is listed below)

## Routing (the classifier and the fallback)

| Measure | Value (numerator/denominator) |
|---|---|
| Requests that published | 13/13 (100%) |
| Valid model classifications / classifier attempts | 13/13 (100%) |
| Model classification accuracy, of the valid ones | 13/13 (100%) |
| Model classification accuracy, of all attempts (a failure counts as not correct) | 13/13 (100%) |
| Classifier failures | 0/13 (0%); by category: none |
| Requests that ran the fallback | 0/13 (0%) (none) |
| Final route correct, fallback included | 13/13 (100%), of which reached only by the fallback: 0 |
| Valid model classifications that differ from the expected route | none |

A request that is right because of the fallback counts as a classifier failure, a fallback execution and a correct final route. It never counts as a model classification.

## Provider failures (run-wide usage ledger)

Failed provider calls: 0/44 (0%) of all recorded calls.

None.

## Cost

* Status: **unpriced**. Cost: **unknown**.
* Tokens (kept even when pricing is missing): generation 21304 in / 2751 out; embeddings 4387 in.
* A ledger or table cost of 0 means not priced, never free. not available: the providers' responses report tokens, not cost.
* Rates: none configured.

## Analysis of the sources

| Source | State | Chunks | Figures | Warnings |
|---|---|---|---|---|
| depots.pdf | ready | 8 | 0 | - |
| march-scan.pdf | ready | 2 | 0 | - |
| handbook.docx | ready | 6 | 1 | - |
| release.md | ready | 4 | 0 | - |

## Items that did not publish

None.

## How to read this

* Gold set: qa-berlin (qa); qa-lisbon-storms (qa); qa-scan-orders (qa); qa-scan-delays (qa); qa-md-owner (qa); qa-md-restart (qa); qa-two-sources (qa); figure-handbook (figure); summarize-release (summarize); compare-depots-release (compare); review-launch-plan (review); diagram-release (diagram); diagram-architecture (diagram).
* "Basis" says what each number actually measures. In stub mode a model-dependent number only shows that the validation and publication code handles a well-behaved answer; it says nothing about how any real model will do. Run `EVAL_PROVIDER=real` for that.
* Small corpus (4 sources, 13 questions, one question per fact): treat differences of a few points as noise.

## What each measure is, and is not

* **Recall@k** counts RETRIEVED PASSAGES (the evidence items sent to the model, chunk-level, ranked by retrieval score; not pages). Expected evidence = the gold item's required phrases (`needles`); a phrase is found when it occurs, normalised for case and spacing, in one of the top-k passages. Items with no phrases are left out of recall.
* **Citation checks are mechanical.** 'No invalid reference or dropped quote' is the system's own validation (every cited label exists; every quote occurs in its passage). It does NOT require an answer to cite anything. 'Cites the expected source' and 'cited text contains the phrase' are string checks.
* **Denominators.** Quality metrics are over PUBLISHED requests; a request that did not publish is listed under 'Items that did not publish' and counts against 'Requests that published'. Provider failures that a request survived are shown under Provider failures, not folded into any rate.
* **Not assessed here:** whether an answer is factually supported by its source, or useful. Nothing in this file is a human judgement of either. The evaluator has no pass/fail threshold; pytest passing means the harness ran.
