# Evaluation results (local provider stubs)

Run: 2026-11-09 20:55 UTC. Gold items: 13; published: 13. Sources: depots.pdf, march-scan.pdf, handbook.docx, release.md.

| Metric | Value | Basis |
|---|---|---|
| Requests that published an answer | 100% (13/13) | measured (real request pipeline) |
| Retrieval recall@1 (mean share of required phrases found) | 65% | measured: real chunking, SQL full-text and ranking; vector part is a topic-bag stand-in |
| Retrieval recall@3 | 95% | same |
| Retrieval recall@5 | 95% | same |
| Evidence recall (anywhere in the evidence sent to the model) | 100% | same |
| Items with every required phrase in the evidence | 100% | same |
| OCR character accuracy (3 scanned pages) | 92% | measured (real Tesseract, identical in both modes) |
| Task type classified as expected | 100% | stub: the classifier answer is scripted |
| Citations without an invalid reference or dropped quote | 100% | stub: NOT a model measurement |
| Answers that cite the expected source(s) | 92% | stub: NOT a model measurement |
| Cited text contains the required phrase | 80% | stub: NOT a model measurement |
| Schema-valid answers without a repair call | 100% | stub: NOT a model measurement |
| Diagram requests that published a diagram | 100% | stub: NOT a model measurement |
| Diagrams that render (Mermaid parses) | 100% | stub: NOT a model measurement |
| Figure questions that cite a figure | 100% | stub: NOT a model measurement |
| Summaries/comparisons with full section coverage | 100% | measured (server-computed coverage) |
| Latency p50 / p95 per request (s) | 0.75 / 3.26 | stub: pipeline time only, no network or model time |

## Per task type

| Task | Items | Published | Calls | Input tokens | Output tokens | Est. cost (USD) | Latency p50 (s) | Basis for tokens/cost |
|---|---|---|---|---|---|---|---|---|
| compare | 1 | 1 | 3.0 | 283 | 60 | 0.00037 | 0.73 | stub placeholder usage |
| diagram | 2 | 2 | 3.0 | 270 | 60 | 0.00037 | 3.19 | stub placeholder usage |
| figure | 1 | 1 | 3.0 | 272 | 60 | 0.00037 | 0.76 | stub placeholder usage |
| qa | 7 | 7 | 3.0 | 268 | 60 | 0.00037 | 0.75 | stub placeholder usage |
| review | 1 | 1 | 2.0 | 240 | 60 | 0.00036 | 0.61 | stub placeholder usage |
| summarize | 1 | 1 | 3.0 | 360 | 90 | 0.00054 | 0.72 | stub placeholder usage |

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
