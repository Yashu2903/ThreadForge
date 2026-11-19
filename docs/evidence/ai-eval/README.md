# AI evaluation evidence (real providers, 2026-11-19)

Sanitized reports, run manifests and per-case records of the real-provider runs. The full results folder
(`backend/tests/eval/results/`) is gitignored and is **not** committed; these are copies chosen for this folder.
They contain no keys, no personal data and no local paths (scanned before committing; the real key values were compared in
memory and appear nowhere). The text in them is model output over a **synthetic** corpus (`backend/tests/eval/corpus.py`).

Providers: OpenAI `gpt-6-luna` (generation, classification, vision and map calls) and Google `gemini-embedding-2` at 1536 dimensions
(embeddings). **Cost is unknown/unconfigured** in every run: no price settings exist, so a ledger or table cost of 0 means "could not be
priced", never "free". Tokens are provider-reported. The account is in `docs/ai.md` 22.2 and 22.7-22.9.

## Fourth stage: classifier effort `none`, the repeat check, the review sheet (2026-11-19, later)

| Files | What they are |
|---|---|
| `none-answer.*` | The 13-question real answer evaluation with `AI_CLASSIFIER_REASONING_EFFORT=none` (`eval-2`, two-stage manifest; the manifest's `configuration` records the effort and the 512-token budget, captured before the first provider call). Compare with `postfix-answer.*`: same code, same dataset, only the classifier effort differs |
| `classifier-repeat.json` / `.md` | A bounded repeat check of the classifier alone (180 calls, cap 200): 8 gold instructions and 10 diagnostic variants, 5 attempts each, effort not sent against `none`. A diagnostic, not part of the acceptance dataset; the adoption rule is in the script's docstring, written before the first run. It uses the production adapter, prompt, schema and budget but not the request pipeline or the ledger |
| `human-review-sheet.md` | 8 answers and 8 proposals (including `diagram-release` twice and the two real repairs) with the request, cited passages, output, expected evidence, the harness's automated checks, my own reading, and **empty** human-review fields. Regenerate with `PYTHONPATH=. uv run python -m tests.eval.build_review_sheet` from `backend/`; it reads only the files in this folder |
| `remaining-backend-tests.json` | Per-file results for batches 10-15 of the backend suite, one process per file (see `docs/ai.md` 23.3) |

## Three stages of runs, and how each was recorded

| Stage | Files | Evaluator | How the evidence was captured |
|---|---|---|---|
| Run 1 | `run1-proposal.*`, `run1-answer.*` | `eval-1` | **Reconstructed after the run.** The `.report.md` files were regenerated from the run's own JSON with the unchanged report code (the originals were overwritten by run 2; the numbers were checked against them). The manifests are rebuilt field by field: each field is labelled `captured_at_completion`, `reconstructed` (with its evidence) or `unavailable` (with the reason) |
| Run 2 | `run2-proposal.*`, `run2-answer.*`, `run2-provenance-note.json` | `eval-1` | Written by the harness **at completion, not at start**. The note says how far the start state can be trusted (believed equal, not machine-verified) |
| Post-fix | `postfix-proposal.*`, `postfix-answer.*`, `*.run-status.json` | `eval-2` | Two-stage manifests: the start-stage fields (run id, time, commit and tree state, dataset, prompt and schema hashes, allowlisted configuration, retry and repair settings, expected item count) were captured **before the first provider call**; the completion stage (usage ledger, failures, cost, metrics) at the end. The exit status is the evaluator's own, recorded by `tests/eval/run_eval.py` |

Other files: `*.records.json` (per-case results as the harness wrote them), `corrections.json` (see below), and the browser-run records
`real-browser-run.json` / `real-recovery-run.json` (first real browser runs) and `postfix-real-browser-run.json` /
`postfix-real-recovery-run.json` (after the fix; they include the routing record and the request-to-proposal links read from the database).

## Original reports are kept; corrections are separate

Run 1 and run 2's reports show "Task type classified as expected 100%" and `Est. cost 0.00000`. Those are the original outputs and are
unchanged. `corrections.json` holds the corrected figures (classifier failures, fallback, final routing, cost) for runs 1 and 2, dated and
labelled as derived after the fact from the original records. The route the model itself chose was **not recorded** by runs 1 or 2, so model
classification accuracy is `unavailable` for them. The post-fix run records it per request.

## What is and is not known about run 1

| Field | Run 1 |
|---|---|
| Code | commit `14e0cf1`; tracked tree equal to it is **reconstructed**: verified for the answer run by a pre-launch `git status` snapshot, inferred for the proposal run |
| Dataset, prompt and schema files | computed afterwards from files that `git diff HEAD` shows unmodified: described as *consistent with 14e0cf1*, not as a record of what the run read |
| Configuration | computed afterwards from today's allowlisted settings. The `.env` file's timestamp (before both runs) is cited only as limited supporting evidence; it does **not** prove the values in force, and the manifest does not claim exact runtime configuration |
| Models | requested: from the same reconstruction. Returned per call: **proposal run unavailable**; **answer run recovered** from usage-ledger rows read from the test database before they were overwritten |
| Request ids, published answer text | unavailable |
| Start time | answer run: a bound only (the pre-launch snapshot time); proposal run unavailable |

## Known limits of the post-fix manifests

* The code fingerprint is a hash of the **tracked** diff outside `docs/`; untracked files (new tests, the runner) are listed by path but not
  hashed. The post-fix code was uncommitted when the runs started; the commit that follows holds those same changes.
* The classifier settings were not yet in the recording allowlist, so the post-fix manifests do not show them. The runs used the defaults
  (`AI_CLASSIFIER_MAX_OUTPUT_TOKENS=512`, effort unset, no override in the `.env`); that statement is made here, not captured. They are
  recorded from now on.
* In `run2-*.manifest.json` the first entry of `code.changed_or_untracked_paths` lost its leading `b` (git's status output was stripped);
  fixed in the harness, the captured files left as written.

## Reproducing a run

```
cd backend
uv run --env-file ../.env python -m tests.eval.run_eval proposal    # the gate; exits with pytest's own status
uv run --env-file ../.env python -m tests.eval.run_eval answer
cd ../frontend && E2E_AI=real npx playwright test e2e/ai-real.spec.ts e2e/ai-real-recovery.spec.ts --project=chromium
PYTHONPATH=. uv run --env-file ../.env python -m tests.eval.rebuild_baseline_manifests   # (backend/) rebuilds run 1's labelled manifests
```

The test and evaluation code **ignores `.env` files** (`make_settings` passes `_env_file=None`), so keys are loaded with `--env-file`.
The harness writes its report to a fixed name, so a later run overwrites the earlier run's markdown; the timestamped JSON and manifest
files are kept. A different run gives different numbers: compare the three stages above.
