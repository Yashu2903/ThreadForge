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
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

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


def cost_status(settings: Any) -> dict[str, Any]:
    configured = {name: getattr(settings, name, None) is not None for name in PRICE_FIELDS}
    known = all(configured.values())
    return {
        "prices_configured": configured,
        "status": "estimated from the configured prices"
        if known
        else "unknown/unconfigured: no price settings",
        "note": (
            None
            if known
            else "The ledger's est_cost_micros is 0 because nothing could be priced. That is NOT a free run and must not be "
            "read as one: the true cost is unknown here."
        ),
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


async def build(
    *,
    kind: str,
    mode: str,
    settings: Any,
    sessionmaker: Any,
    started: dt.datetime,
    dataset: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    ledger = await ledger_summary(sessionmaker, started)
    finished = dt.datetime.now(dt.UTC)
    return {
        "manifest_version": 1,
        "kind": kind,
        "mode": mode,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "captured": "during the run, by the harness",
        "code": git_state(),
        "runtime": {"python": sys.version.split()[0], "platform": platform.platform()},
        "dataset": dataset,
        "prompts_and_schemas": prompts_and_schemas(),
        "configuration": safe_config(settings),
        "models": {
            "requested": {
                "generation": getattr(settings, "ai_llm_model", None),
                "embedding": getattr(settings, "ai_embed_model", None),
            },
            "recorded_by_the_usage_ledger": sorted(
                {f"{r['provider']}:{r['model']}" for r in ledger}
            ),
        },
        "usage_ledger_run_wide": ledger,
        "usage_totals": {
            "calls": sum(int(r["calls"]) for r in ledger),
            "failed_calls": sum(int(r["calls"]) for r in ledger if r["status"] != "ok"),
            "input_tokens": sum(int(r["input_tokens"]) for r in ledger),
            "output_tokens": sum(int(r["output_tokens"]) for r in ledger),
        },
        "cost": cost_status(settings),
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
                )
            }
            for r in records
        ],
    }


def write(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
