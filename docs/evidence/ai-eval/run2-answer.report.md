# Evaluation results (REAL providers)

Run: 2026-11-19 15:34 UTC. Gold items: 13; published: 13. Sources: depots.pdf, march-scan.pdf, handbook.docx, release.md.

| Metric | Value | Basis |
|---|---|---|
| Requests that published an answer | 100% (13/13) | measured (real request pipeline) |
| Retrieval recall@1 (mean share of required phrases found) | 85% | measured |
| Retrieval recall@3 | 100% | same |
| Retrieval recall@5 | 100% | same |
| Evidence recall (anywhere in the evidence sent to the model) | 100% | same |
| Items with every required phrase in the evidence | 100% | same |
| OCR character accuracy (3 scanned pages) | 92% | measured (real Tesseract, with the vision fallback available) |
| Task type classified as expected | 100% | measured |
| Citations without an invalid reference or dropped quote | 100% | measured |
| Answers that cite the expected source(s) | 100% | measured |
| Cited text contains the required phrase | 100% | measured |
| Schema-valid answers without a repair call | 100% | measured |
| Diagram requests that published a diagram | 100% | measured |
| Diagrams that render (Mermaid parses) | 100% | measured |
| Figure questions that cite a figure | 100% | measured |
| Summaries/comparisons with full section coverage | 100% | measured (server-computed coverage) |
| Latency p50 / p95 per request (s) | 5.07 / 10.51 | measured, end to end |

## Per task type

| Task | Items | Published | Calls | Input tokens | Output tokens | Est. cost (USD) | Latency p50 (s) | Basis for tokens/cost |
|---|---|---|---|---|---|---|---|---|
| compare | 1 | 1 | 3.0 | 2647 | 768 | 0.00000 | 10.51 | provider-reported |
| diagram | 2 | 2 | 3.0 | 1169 | 194 | 0.00000 | 7.78 | provider-reported |
| figure | 1 | 1 | 3.0 | 1619 | 105 | 0.00000 | 4.57 | provider-reported |
| qa | 7 | 7 | 3.0 | 1716 | 118 | 0.00000 | 5.07 | provider-reported |
| review | 1 | 1 | 2.0 | 956 | 232 | 0.00000 | 4.64 | provider-reported |
| summarize | 1 | 1 | 3.0 | 1195 | 398 | 0.00000 | 10.11 | provider-reported |

(per-item means across the request's own calls; analysis of the sources is listed below)

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
