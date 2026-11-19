"""A run manifest for the evaluation harness: what ran, on which code, data and configuration, and what it used.

It RECORDS; it never changes a question, an expected phrase, a score or a threshold. A manifest is written next to each
results file (`<name>.manifest.json`). Nothing secret can get in: configuration goes through an explicit allowlist of
setting NAMES, and prices are recorded only as "configured or not".

Fields that cannot be known are written as the string "unavailable: <why>", never guessed and never left to look like a value.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
import subprocess
import sys
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.ai.pricing import Prices, cost_report
from tests.eval.metrics import failed_calls

REPO = Path(__file__).resolve().parents[3]
BACKEND = REPO / "backend"

# the only settings that are recorded: names, never values of keys or secrets
CONFIG_ALLOWLIST = (
    "ai_llm_model",
    "ai_embed_model",
    "ai_embed_dimensions",
    "ai_embed_max_input_tokens",
    "ai_llm_context_tokens",
    "ai_llm_max_output_tokens",
    "ai_llm_structured_output",
    "ai_llm_reasoning_effort",
    "ai_classifier_max_output_tokens",
    "ai_classifier_reasoning_effort",
    "ai_max_input_tokens",
    "ai_max_output_tokens",
    "ai_max_provider_calls",
    "ai_call_timeout_seconds",
    "ai_retry_max_attempts",
    "ai_key_concurrency",
    "ai_key_policy",
    "ai_proposals_enabled",
    "ai_retrieval_lexical_k",
    "ai_retrieval_vector_k",
    "ai_retrieval_rrf_k",
    "ai_retrieval_evidence_tokens",
    "ai_retrieval_max_distance",
    "ai_chunk_target_tokens",
    "ai_chunk_max_tokens",
    "ai_chunk_overlap_tokens",
    "ai_openai_base_url",
    "ai_google_base_url",
    "ai_prices_checked_on",
)
PRICE_FIELDS = (
    "ai_llm_input_micros_per_mtok",
    "ai_llm_output_micros_per_mtok",
    "ai_embed_text_micros_per_mtok",
    "ai_embed_image_micros_per_mtok",
)


def unavailable(why: str) -> str:
    return f"unavailable: {why}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_json(obj: Any) -> str:
    def default(o: Any) -> Any:
        if is_dataclass(o) and not isinstance(o, type):
            return asdict(o)
        return str(o)

    return sha256_bytes(json.dumps(obj, sort_keys=True, default=default).encode("utf-8"))


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)  # noqa: S603, S607
    # only trailing whitespace goes: `git status --porcelain` lines start with a significant space (" M path")
    return out.stdout.rstrip() if out.returncode == 0 else ""


def git_state() -> dict[str, Any]:
    """The commit and the state of the working tree: which files differ, and a hash of the tracked changes."""
    status = [line for line in _git("status", "--porcelain").splitlines() if line]
    # the evaluation's own outputs and local secrets are not code
    status = [
        line for line in status if "tests/eval/results/" not in line and not line.endswith(" .env")
    ]
    diff = _git("diff", "HEAD", "--", ".", ":(exclude)docs")
    return {
        "commit": _git("rev-parse", "HEAD") or unavailable("not a git checkout"),
        "branch": _git("branch", "--show-current") or unavailable("detached or not a git checkout"),
        "working_tree_clean": not status,
        "changed_or_untracked_paths": [line[3:] for line in status],
        "tracked_code_diff_sha256": sha256_bytes(diff.encode("utf-8"))
        if diff
        else "no tracked changes outside docs",
    }


def prompts_and_schemas() -> dict[str, Any]:
    """Identifiers and content hashes of the prompts and schemas the runs depend on."""
    from app.ai.analysis.jobs import PARSER_VERSION
    from app.ai.answer import prompts as answer_prompts
    from app.ai.edit import prompts as edit_prompts

    files = {
        "answer_prompts": BACKEND / "app/ai/answer/prompts.py",
        "answer_schema_and_validation": BACKEND / "app/ai/answer/answer.py",
        "edit_prompts": BACKEND / "app/ai/edit/prompts.py",
        "edit_schema": BACKEND / "app/ai/edit/schema.py",
    }
    return {
        "answer_prompt_template_version": answer_prompts.PROMPT_TEMPLATE_VERSION,
        "edit_prompt_template_version": edit_prompts.PROMPT_TEMPLATE_VERSION,
        "parser_version": PARSER_VERSION,
        "classify_schema_sha256": sha256_json(answer_prompts.CLASSIFY_SCHEMA),
        "file_sha256": {
            name: sha256_file(p) if p.exists() else unavailable("file missing")
            for name, p in files.items()
        },
    }


def safe_config(settings: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for name in CONFIG_ALLOWLIST:
        value = getattr(settings, name, None)
        cfg[name] = (
            "default (provider's own endpoint)"
            if name.endswith("_base_url") and not value
            else value
        )
    return cfg


EVALUATOR_VERSION = "eval-2"
# eval-1: runs 1 and 2 (routing known only as "task type classified as expected"; cost shown as 0 when unpriced).
# eval-2: routing outcomes with numerators and denominators, failed provider calls by category, explicit cost status,
#         and manifests captured in two stages (start, completion).

PROVENANCE = ("captured_at_start", "captured_at_completion", "reconstructed", "unavailable")


def cost_summary(settings: Any, ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """What this run cost, stated with what could and could not be priced. Computed from the run-wide usage ledger ONLY
    (one source, so nothing is counted twice; per-request aggregates are views of the same rows).

    A ledger cost of 0 is NOT "free": when no rate is configured the cost of a call that used tokens is unknown. Only
    usage of nothing is a known zero. Tokens are always retained. The providers' responses report tokens, not money, so
    a provider-reported cost does not exist here."""
    prices = Prices.of(settings)
    gen_in = sum(int(r["input_tokens"]) for r in ledger if r["provider"] == "openai")
    gen_out = sum(int(r["output_tokens"]) for r in ledger if r["provider"] == "openai")
    embed = sum(int(r["input_tokens"]) for r in ledger if r["provider"] == "google")
    report = cost_report(prices, llm_in=gen_in, llm_out=gen_out, embed_tokens=embed)
    micros = report["est_cost_micros"]
    rates = {
        "generation_input_micros_per_mtok": prices.llm_input,
        "generation_output_micros_per_mtok": prices.llm_output,
        "embedding_text_micros_per_mtok": prices.embed_text,
    }
    return {
        "status": report["cost_status"],
        "complete_total": report["cost_status"] in ("estimated", "none"),
        "est_cost_micros": micros,
        "est_cost_usd": None if micros is None else micros / 1_000_000,
        "currency": "USD",
        "unit_note": "micro-dollars: 1,000,000 = 1 USD",
        "unpriced_components": report["unpriced_components"],
        "kind_of_value": {
            "none": "known zero: nothing was used",
            "estimated": "estimated from the configured rates below (not provider-reported)",
            "partial": "PARTIAL: the priced components only; the unpriced components are missing from this figure",
            "unpriced": "unknown: usage was recorded but no rate is configured (the ledger's 0 means 'not priced')",
        }[report["cost_status"]],
        "rates": {
            **rates,
            "source": "the configured rates (AI_*_MICROS_PER_MTOK)",
            "checked_on": getattr(settings, "ai_prices_checked_on", "") or None,
        }
        if any(v is not None for v in rates.values())
        else None,
        "provider_reported_cost": "not available: the providers' responses report tokens, not cost",
        "usage_retained": {
            "generation_input_tokens": gen_in,
            "generation_output_tokens": gen_out,
            "embedding_input_tokens": embed,
        },
    }


async def ledger_summary(sessionmaker: Any, since: dt.datetime) -> list[dict[str, Any]]:
    """What the usage ledger recorded since the run started, grouped by operation, provider, model and status.
    The model column is what the adapter recorded for the call (the provider's reply where it reports one)."""
    async with sessionmaker() as db:
        got = await db.execute(
            text(
                "SELECT operation, provider, model, status, coalesce(error_code,'-') AS error_code, count(*) AS calls, "
                "coalesce(sum(input_tokens),0) AS input_tokens, coalesce(sum(output_tokens),0) AS output_tokens, "
                "coalesce(sum(est_cost_micros),0) AS est_cost_micros "
                "FROM ai_usage_ledger WHERE created_at >= :since GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5"
            ),
            {"since": since},
        )
        return [dict(r._mapping) for r in got]


def retry_and_repair(settings: Any, kind: str) -> dict[str, Any]:
    return {
        "retried_provider_codes": [
            "provider_rate_limited",
            "provider_unavailable",
            "provider_timeout",
        ],
        "max_attempts_per_call": getattr(settings, "ai_retry_max_attempts", None),
        "base_delay_seconds": getattr(settings, "ai_retry_base_delay_seconds", None),
        "max_delay_seconds": getattr(settings, "ai_retry_max_delay_seconds", None),
        "call_timeout_seconds": getattr(settings, "ai_call_timeout_seconds", None),
        "truncated_output_retried": False,
        "repairs": "at most one repair request per proposal (the gate's definition)"
        if kind == "proposal"
        else "none for answers: a reply that fails validation fails the request",
    }


def begin(
    *,
    kind: str,
    mode: str,
    settings: Any,
    dataset: dict[str, Any],
    expected_items: int,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Stage 1: everything knowable BEFORE the first provider call, captured at the start."""
    started = now or dt.datetime.now(dt.UTC)
    return {
        "manifest_version": 2,
        "evaluator_version": EVALUATOR_VERSION,
        "run_id": uuid.uuid4().hex,
        "kind": kind,
        "mode": mode,
        "started_utc": started.isoformat(),
        "expected_items": expected_items,
        "captured_at_start": [
            "run_id",
            "started_utc",
            "evaluator_version",
            "expected_items",
            "code",
            "runtime",
            "dataset",
            "prompts_and_schemas",
            "configuration",
            "retry_and_repair",
            "models_requested",
        ],
        "code": git_state(),
        "runtime": {"python": sys.version.split()[0], "platform": platform.platform()},
        "dataset": dataset,
        "prompts_and_schemas": prompts_and_schemas(),
        "configuration": safe_config(settings),
        "retry_and_repair": retry_and_repair(settings, kind),
        "models_requested": {
            "generation": getattr(settings, "ai_llm_model", None),
            "embedding": getattr(settings, "ai_embed_model", None),
        },
    }


async def finish(
    m: dict[str, Any],
    *,
    settings: Any,
    sessionmaker: Any,
    records: list[dict[str, Any]],
    results: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Stage 2: what only exists once the run is over (status, usage, models the provider side recorded, metrics)."""
    ledger = await ledger_summary(sessionmaker, dt.datetime.fromisoformat(m["started_utc"]))
    by_provider: dict[str, dict[str, int]] = {}
    for r in ledger:
        p = by_provider.setdefault(
            r["provider"], {"calls": 0, "failed_calls": 0, "input_tokens": 0, "output_tokens": 0}
        )
        p["calls"] += int(r["calls"])
        p["failed_calls"] += int(r["calls"]) if r["status"] != "ok" else 0
        p["input_tokens"] += int(r["input_tokens"])
        p["output_tokens"] += int(r["output_tokens"])
    m.update(
        {
            "finished_utc": (now or dt.datetime.now(dt.UTC)).isoformat(),
            "captured_at_completion": [
                "finished_utc",
                "cases",
                "results",
                "models_recorded",
                "usage_ledger_run_wide",
                "usage_totals",
                "provider_failures",
                "cost",
            ],
            "models_recorded": sorted({f"{r['provider']}:{r['model']}" for r in ledger}),
            "usage_ledger_run_wide": ledger,
            "usage_totals": {
                "source": "the run-wide usage ledger only (per-case figures are views of the same rows)",
                "by_provider": by_provider,
            },
            "provider_failures": failed_calls(ledger),
            "cost": cost_summary(settings, ledger),
            "results": results or {},
            "cases": [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "request_id",
                        "published",
                        "outcome",
                        "first_try_valid",
                        "repaired",
                        "latency_s",
                        "routing",
                        "expected_route",
                    )
                    if k in r
                }
                for r in records
            ],
        }
    )
    return m


def labelled(value: Any, provenance: str, evidence: str | None = None) -> dict[str, Any]:
    """One manifest field with where it came from. `unavailable` fields hold no value, only the reason."""
    if provenance not in PROVENANCE:
        raise ValueError(f"unknown provenance {provenance!r}")
    if provenance == "unavailable":
        if not (isinstance(value, str) and value.startswith("unavailable")):
            raise ValueError("an unavailable field must say why: 'unavailable: <reason>'")
    elif evidence is None and provenance == "reconstructed":
        raise ValueError("a reconstructed field must state its evidence")
    out: dict[str, Any] = {"value": value, "provenance": provenance}
    if evidence is not None:
        out["evidence"] = evidence
    return out


def reconstructed_manifest(
    *, kind: str, mode: str, run_ended: str, fields: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """A manifest assembled AFTER a run that did not capture one. It says so, and every field in it is labelled with its
    provenance (captured / reconstructed with its evidence / unavailable); an unlabelled field is refused."""
    for name, f in fields.items():
        if not (isinstance(f, dict) and f.get("provenance") in PROVENANCE):
            raise ValueError(f"field {name!r} is not labelled with a provenance")
    return {
        "manifest_version": 2,
        "evaluator_version": "eval-1",
        "created": {
            "stage": "after_execution",
            "meaning": "this manifest was built after the run, from the evidence named per field; the run did not capture it",
        },
        "kind": kind,
        "mode": mode,
        "run_ended": run_ended,
        "fields": fields,
    }


def write(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
