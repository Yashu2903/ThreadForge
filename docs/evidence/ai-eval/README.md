# AI evaluation evidence (real providers, 2026-11-19)

Sanitized reports, run manifests and per-case records of the real-provider runs. The full results folder
(`backend/tests/eval/results/`) is gitignored and is **not** committed; these are copies chosen for this folder.
They contain no keys, no personal data and no local paths (scanned before committing; the real key values were compared
in memory and appear nowhere). The text in them is model output over a **synthetic** corpus (`backend/tests/eval/corpus.py`).

Providers: OpenAI `gpt-6-luna` (generation, the answer, classification, vision and map calls) and Google `gemini-embedding-2`
at 1536 dimensions (embeddings). Cost is **unknown/unconfigured**: no price settings exist, so every `cost_micros: 0` or
`Est. cost 0.00000` is "could not be priced", not "free". Token counts are provider-reported.

## Files

| Files | What | How it was captured |
|---|---|---|
| `run1-proposal.*`, `run1-answer.*` | First real run of each evaluation | **Reconstructed.** Run 1 predates the harness's own capture. The `.report.md` files were regenerated from the run's own JSON with the unchanged report code (the originals were overwritten by run 2; the numbers were checked against them). The `.manifest.json` files are built after the fact; every field says how it was obtained, and what could not be recovered is marked `unavailable: <why>` |
| `run2-proposal.*`, `run2-answer.*` | Second real run of each evaluation | **Captured during the run** by `backend/tests/eval/manifest.py`: commit, working-tree state, dataset fingerprints, prompt and schema identifiers and hashes, configuration (allowlisted names only), requested and recorded models, the run-wide usage ledger, request ids, per-case results, and (answer run) the published answers with their citations |
| `*.records.json` | Per-case results (the harness's own JSON) | by the harness |
| `real-browser-run.json` | The normal-interface workflow, three browsers, real inference | spec-recorded identifiers and observed results, plus database facts read straight after the run |
| `real-recovery-run.json` | Offline text recovery with a real model's replacement | same |

## What is and is not known about run 1

| Field | Run 1 |
|---|---|
| Code | commit `14e0cf1`, tracked tree equal to it (verified for the answer run by a pre-launch `git status` snapshot; inferred for the proposal run from that snapshot and the first docs edit at 15:21:53Z, after the run) |
| Dataset, prompts, schemas, configuration | computed afterwards from files byte-identical to the commit, and from the `.env` last written before the run |
| Requested model / returned model per call | requested: known. Returned per call: **proposal run unavailable** (its ledger rows were not preserved); **answer run recovered** from the test database's ledger before it was overwritten |
| Request ids | unavailable (not recorded then) |
| Published answer text | unavailable for run 1 (only metrics were recorded); present for run 2 |
| Start time | unavailable (finish time and pytest duration only) |

## Known glitch in the captured run-2 manifests

In `run2-*.manifest.json` the first entry of `code.changed_or_untracked_paths` reads `ackend/tests/eval/test_eval.py`: the
leading `b` was lost by stripping git's status output (the leading space is significant). The harness is fixed; the captured
files were left exactly as written. Read that entry as `backend/tests/eval/test_eval.py`.

## Reproducing a run

```
cd backend
RUN_EVAL=1 EVAL_PROVIDER=real uv run --env-file ../.env python -m pytest tests/eval/test_proposal_eval.py -s   # the gate
RUN_EVAL=1 EVAL_PROVIDER=real uv run --env-file ../.env python -m pytest tests/eval/test_eval.py -s            # answers
cd ../frontend && E2E_AI=real npx playwright test e2e/ai-real.spec.ts e2e/ai-real-recovery.spec.ts --project=chromium
```

The test and evaluation code **ignores `.env` files** (`make_settings` passes `_env_file=None`), which is why `--env-file` is
needed. The harness writes its report to a fixed name, so a second run overwrites the first's markdown; the timestamped JSON
and manifest files are kept. A different run gives different numbers: see the variation between run 1 and run 2.
