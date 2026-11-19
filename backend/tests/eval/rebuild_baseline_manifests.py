"""Rebuild the baseline (run 1 and run 2) provenance records in the labelled format, and the corrected summaries.

    cd backend
    PYTHONPATH=. uv run --env-file ../.env python -m tests.eval.rebuild_baseline_manifests

Run 1 of each evaluation predates the harness's own capture, so its manifest is reconstructed AFTER the run, field by field,
each labelled `reconstructed` (with its evidence), `captured_at_completion` (a value the harness itself wrote) or
`unavailable` (with the reason). Nothing is claimed as captured that was not. Run 2's manifests were written by the harness
at completion, not start; `run2-provenance-note.json` says so and says how far its start state can be trusted.

The ORIGINAL reports and records of both runs are left exactly as they were. The corrected figures (classifier failures,
fallback, cost status) go in `corrections.json`, dated and labelled as derived after the fact from the original records.
"""

from __future__ import annotations

import json
from typing import Any

from tests.eval import corpus, manifest
from tests.eval.metrics import fraction
from tests.eval.proposals import GOLD as PROPOSAL_GOLD
from tests.factories import make_settings

REPO = manifest.REPO
OUT = REPO / "docs/evidence/ai-eval"
COMMIT = "14e0cf12aa8765426877fc7f5bf867eebbb1d8f6"
L = manifest.labelled

# usage-ledger rows of answer run 1, read from the test database after the run and before the next run overwrote them
ANSWER_RUN1_LEDGER = [
    {"operation": "answer", "provider": "openai", "model": "gpt-6-luna", "status": "ok", "error_code": "-", "calls": 13, "input_tokens": 18700, "output_tokens": 2341},
    {"operation": "classify", "provider": "openai", "model": "gpt-6-luna", "status": "ok", "error_code": "-", "calls": 12, "input_tokens": 1640, "output_tokens": 210},
    {"operation": "classify", "provider": "openai", "model": "gpt-6-luna", "status": "failed", "error_code": "provider_output_truncated", "calls": 1, "input_tokens": 0, "output_tokens": 0},
    {"operation": "embed_text", "provider": "google", "model": "gemini-embedding-2", "status": "ok", "error_code": "-", "calls": 16, "input_tokens": 4386, "output_tokens": 0},
    {"operation": "map", "provider": "openai", "model": "gpt-6-luna", "status": "ok", "error_code": "-", "calls": 1, "input_tokens": 226, "output_tokens": 79},
    {"operation": "vision_describe", "provider": "openai", "model": "gpt-6-luna", "status": "ok", "error_code": "-", "calls": 1, "input_tokens": 603, "output_tokens": 129},
]  # fmt: skip

CODE_EVIDENCE = (
    "Reconstructed after the run. Answer run: a `git status` snapshot taken at 15:26:05Z, immediately before launch, shows HEAD "
    "14e0cf1 and only docs/ai.md and docs/ai-progress.md modified. Proposal run (finished 15:20:29Z): no snapshot was taken; "
    "inferred from the same snapshot and from docs/ai.md's first modification at 15:21:53Z, after the run. Documentation does not "
    "affect either evaluation."
)
FILES_EVIDENCE = (
    "Computed after the run from the files as they are now. `git diff HEAD` is empty for these files, so they are CONSISTENT WITH "
    "commit 14e0cf1; that is a statement about the files, not a record of what the run read."
)
CONFIG_EVIDENCE = (
    "Computed after the run from the allowlisted settings of today's .env plus the harness overrides. LIMITED evidence only: the "
    ".env file was last written at 15:17:10Z, before both runs, but a file timestamp does not prove which values were in force "
    "during a run, so this is NOT claimed as the exact runtime configuration."
)


def settings(proposals: bool):
    return make_settings(ai_enabled=True, ai_proposals_enabled=proposals)


def agg(records: list[dict[str, Any]]) -> dict[str, int]:
    keys = ("calls", "answer_calls", "input_tokens", "output_tokens", "failed_calls")
    return {k: sum(r["ledger"].get(k, 0) or 0 for r in records) for k in keys}


def proposal_run1() -> dict[str, Any]:
    d = json.loads(
        (REPO / "backend/tests/eval/results/proposals-real-20261119-152029.json").read_text(
            encoding="utf-8"
        )
    )
    recs = d["records"]
    cfg = settings(True)
    usage = agg(recs)
    fields = {
        "run_ended": L(
            "2026-11-19T15:20:29Z",
            "captured_at_completion",
            "the harness's own results file name stamp (UTC)",
        ),
        "started_utc": L(
            "unavailable: not recorded; only the finish time and the pytest duration (143 s) are known",
            "unavailable",
        ),
        "evaluator_version": L("eval-1", "reconstructed", "the evaluator code at commit 14e0cf1"),
        "expected_items": L(
            16, "reconstructed", "the gold list at commit 14e0cf1 has 16 items; 16 records exist"
        ),
        "code": L(
            {"commit": COMMIT, "tracked_tree_equals_commit": True}, "reconstructed", CODE_EVIDENCE
        ),
        "dataset": L(
            {
                "items": len(PROPOSAL_GOLD),
                "gold_sha256": manifest.sha256_json(list(PROPOSAL_GOLD)),
                "proposals_py_sha256": manifest.sha256_file(
                    REPO / "backend/tests/eval/proposals.py"
                ),
                "corpus_py_sha256": manifest.sha256_file(REPO / "backend/tests/eval/corpus.py"),
                "sources_sha256": {
                    n: manifest.sha256_bytes(corpus.sources()[n])
                    for n in ("release.md", "depots.pdf")
                },
            },
            "reconstructed",
            FILES_EVIDENCE,
        ),
        "prompts_and_schemas": L(manifest.prompts_and_schemas(), "reconstructed", FILES_EVIDENCE),
        "configuration": L(
            manifest.safe_config(cfg),
            "reconstructed",
            CONFIG_EVIDENCE + " No independent evidence of the model in use exists for this run.",
        ),
        "models_requested": L(
            {"generation": cfg.ai_llm_model, "embedding": cfg.ai_embed_model},
            "reconstructed",
            CONFIG_EVIDENCE,
        ),
        "models_returned_per_call": L(
            "unavailable: this run's per-call usage-ledger rows were not preserved (the test database was overwritten by the next evaluation)",
            "unavailable",
        ),
        "request_ids": L("unavailable: not recorded by the harness at the time", "unavailable"),
        "retry_and_repair": L(
            manifest.retry_and_repair(cfg, "proposal"),
            "reconstructed",
            "the retry settings are defaults in app/config.py at commit 14e0cf1 and are not set in the .env",
        ),
        "usage_request_attributed": L(
            usage,
            "captured_at_completion",
            "summed from the per-case ledger the harness wrote into the results JSON; source-analysis calls made before the requests are NOT included",
        ),
        "cost": L(
            {
                "status": "unpriced",
                "est_cost_micros": None,
                "note": "no price settings exist in the .env, so usage could not be priced; the ledger's 0 means not priced. Embedding usage during source analysis is unavailable, so this run's token usage is incomplete",
            },
            "reconstructed",
            "the price settings are unset in the .env (computed from its allowlisted names); the per-case ledger shows cost 0 with tokens above 0, which is only possible when unpriced",
        ),
        "cases": L(
            [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "published",
                        "outcome",
                        "first_try_valid",
                        "repaired",
                        "latency_s",
                    )
                }
                for r in recs
            ],
            "captured_at_completion",
            "from the results JSON",
        ),
    }
    return manifest.reconstructed_manifest(
        kind="proposal", mode="real", run_ended="2026-11-19T15:20:29Z", fields=fields
    )


def answer_run1() -> dict[str, Any]:
    d = json.loads(
        (REPO / "backend/tests/eval/results/eval-real-20261119-152748.json").read_text(
            encoding="utf-8"
        )
    )
    recs = d["records"]
    cfg = settings(False)
    usage = agg(recs)
    fields = {
        "run_ended": L(
            "2026-11-19T15:27:48Z",
            "captured_at_completion",
            "the harness's own results file name stamp (UTC)",
        ),
        "started_utc": L(
            "2026-11-19T15:26:05Z",
            "reconstructed",
            "the time of the pre-launch git snapshot, taken immediately before the run command; the run's own start was not recorded, so treat this as a bound, not the start",
        ),
        "evaluator_version": L("eval-1", "reconstructed", "the evaluator code at commit 14e0cf1"),
        "expected_items": L(
            13, "reconstructed", "the gold list at commit 14e0cf1 has 13 items; 13 records exist"
        ),
        "code": L(
            {"commit": COMMIT, "tracked_tree_equals_commit": True}, "reconstructed", CODE_EVIDENCE
        ),
        "dataset": L(
            {
                "questions": len(corpus.GOLD),
                "gold_sha256": manifest.sha256_json(list(corpus.GOLD)),
                "corpus_py_sha256": manifest.sha256_file(REPO / "backend/tests/eval/corpus.py"),
                "sources_sha256": {
                    n: manifest.sha256_bytes(b) for n, b in corpus.sources().items()
                },
            },
            "reconstructed",
            FILES_EVIDENCE,
        ),
        "prompts_and_schemas": L(manifest.prompts_and_schemas(), "reconstructed", FILES_EVIDENCE),
        "configuration": L(manifest.safe_config(cfg), "reconstructed", CONFIG_EVIDENCE),
        "models_requested": L(
            {"generation": cfg.ai_llm_model, "embedding": cfg.ai_embed_model},
            "reconstructed",
            CONFIG_EVIDENCE,
        ),
        "models_recorded_by_the_usage_ledger": L(
            ["google:gemini-embedding-2", "openai:gpt-6-luna"],
            "reconstructed",
            "usage-ledger rows read from the test database after the run and before the next run overwrote them (created 15:26:17Z-15:27:45Z, inside the run window)",
        ),
        "usage_ledger_run_wide": L(
            ANSWER_RUN1_LEDGER, "reconstructed", "the same recovered ledger rows"
        ),
        "request_ids": L("unavailable: not recorded by the harness at the time", "unavailable"),
        "published_answers": L(
            "unavailable: run 1 recorded metrics only, not the answer text", "unavailable"
        ),
        "retry_and_repair": L(
            manifest.retry_and_repair(cfg, "answer"),
            "reconstructed",
            "the retry settings are defaults in app/config.py at commit 14e0cf1 and are not set in the .env",
        ),
        "usage_request_attributed": L(
            usage,
            "captured_at_completion",
            "summed from the per-case ledger the harness wrote into the results JSON",
        ),
        "cost": L(
            manifest.cost_summary(cfg, ANSWER_RUN1_LEDGER),
            "reconstructed",
            "computed after the run from the recovered ledger rows and the (unset) price settings",
        ),
        "cases": L(
            [{k: r.get(k) for k in ("id", "published", "outcome", "latency_s")} for r in recs],
            "captured_at_completion",
            "from the results JSON",
        ),
    }
    return manifest.reconstructed_manifest(
        kind="answer", mode="real", run_ended="2026-11-19T15:27:48Z", fields=fields
    )


def corrections() -> dict[str, Any]:
    """Corrected figures for the first two runs, derived after the fact from their ORIGINAL records. The original reports are
    not changed. The routes the model itself chose were not recorded by either run, so model classification accuracy is
    unavailable for both."""
    out: dict[str, Any] = {
        "created": "2026-11-19, after the runs; derived from the original records, not captured by the runs",
        "why": "the original reports showed 'Task type classified as expected 100%', which counted the fallback as a correct "
        "classification and hid the failed classifier calls, and showed 0 cost for unpriced usage",
        "runs": {},
    }
    for label, name in (
        ("run1", "eval-real-20261119-152748.json"),
        ("run2", "eval-real-20261119-153442.json"),
    ):
        d = json.loads((REPO / "backend/tests/eval/results" / name).read_text(encoding="utf-8"))
        recs = d["records"]
        failed = [r["id"] for r in recs if r["ledger"]["failed_calls"]]
        out["runs"][label] = {
            "classification_attempts": L(
                len(recs),
                "reconstructed",
                "every gold item was sent with task_hint=auto (the evaluator at 14e0cf1 sets it)",
            ),
            "classifier_failures": L(
                {
                    **fraction(len(failed), len(recs)),
                    "category": "truncated (provider_output_truncated)",
                    "requests": failed,
                },
                "reconstructed",
                "the per-case ledger shows failed_calls=1 on exactly these requests, and the recovered/captured run-wide ledger shows the failed rows are classify calls with that code; inferred, not recorded per request",
            ),
            "valid_model_classifications": L(
                fraction(len(recs) - len(failed), len(recs)),
                "reconstructed",
                "attempts minus the failed classifier calls",
            ),
            "fallback_requests": L(
                {**fraction(len(failed), len(recs)), "requests": failed},
                "reconstructed",
                "a failed classification means qa by design (app/ai/answer/execute.py at 14e0cf1); these requests still published",
            ),
            "final_route_accuracy": L(
                fraction(sum(1 for r in recs if r["task_ok"]), len(recs)),
                "captured_at_completion",
                "task_ok in the original records (the FINAL route, fallback included)",
            ),
            "model_classification_accuracy": L(
                "unavailable: the route the model itself returned was not recorded by this run",
                "unavailable",
            ),
            "cost": L(
                "unknown/unconfigured: the ledger's 0 meant 'not priced' (no price settings); the original reports' 0.00000 must not be read as free",
                "reconstructed",
                "price settings unset in the .env",
            ),
        }
    return out


def run2_note() -> dict[str, Any]:
    fields = {
        "manifest_stage": L(
            "The run-2 manifests were written by the harness at COMPLETION, not at start: their code, dataset, prompt and configuration fields describe the state when the run ended.",
            "captured_at_completion",
        ),
        "start_state_equals_completion_state": L(
            "believed, not machine-verified",
            "reconstructed",
            "the operator log of this session shows no repository edits between launch (15:30:25Z, the manifest's own started_utc) and completion (15:34:42Z); no start-time snapshot of the working tree exists for run 2",
        ),
        "known_glitch": L(
            "the first entry of code.changed_or_untracked_paths lost its leading 'b' (git status output was stripped); fixed afterwards in manifest.py",
            "captured_at_completion",
        ),
        "evaluator_version": L(
            "eval-1",
            "reconstructed",
            "run 2 used the harness before the routing-aware reporting; it recorded no per-request routing and no model route",
        ),
    }
    return manifest.reconstructed_manifest(
        kind="note", mode="real", run_ended="2026-11-19T15:34:42Z", fields=fields
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (
        ("run1-proposal.manifest.json", proposal_run1),
        ("run1-answer.manifest.json", answer_run1),
        ("run2-provenance-note.json", run2_note),
    ):
        manifest.write(OUT / name, build())
    (OUT / "corrections.json").write_text(json.dumps(corrections(), indent=2), encoding="utf-8")
    print("baseline provenance records written to", OUT)


if __name__ == "__main__":
    main()
