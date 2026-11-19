"""Bounded repeat check of the task classifier at two reasoning efforts. A DIAGNOSTIC, not an acceptance test.

    cd backend
    uv run --env-file ../.env python -m tests.eval.diagnostics.classifier_repeat [--repeats 5]

What it does: sends each instruction (a few gold instructions read from `corpus.GOLD`, plus the labelled variants in
`variants.py`) to the real classifier REPEATS times at each of two arms, and counts the routes returned:

  default   effort NOT sent (the provider's own default for the model; this was the setting of the post-fix run)
  none      effort `none` (the candidate)

It uses the production adapter (`OpenAILLM.generate_structured`), the production prompt and schema, and the production output
budget (`AI_CLASSIFIER_MAX_OUTPUT_TOKENS`). It does NOT go through the request pipeline, the call policy or the ledger: the
classifier sees the instruction text alone, so nothing else could change its answer. Each attempt is ONE call; there are no
retries here, and a failed call is counted as a failure, not retried.

It never touches `corpus.GOLD`, the prompts, the enum, the fallback or the generation settings. The call count is capped
(MAX_CALLS) before any call is made.

DECISION RULE, written before the first run (docs/ai.md 23.1). `none` is adopted as the default only if ALL of these hold:
  1. the full 13-question answer evaluation with `none` has 0 classifier failures, 13/13 final routes as expected, and no item
     whose retrieval or citation result is worse than in the post-fix run;
  2. in this repeat check `none` returns the expected route on every attempt for `diagram-release`;
  3. for every route, `none` is correct on at least as many attempts as `default` is (counts, not rates), and has 0 failures;
  4. no gold instruction is routed correctly by `default` on every attempt and wrongly by `none` on any.
If any fails, the result is reported and the existing configuration is kept.

The output records the arm settings, counts with denominators, tokens and latency per arm, and the cost status (unknown when no
prices are configured, never zero). No keys, no provider response bodies: only the route value, a failure code, token counts.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any

from app.ai import errors
from app.ai.answer import prompts
from app.ai.answer.execute import ROUTES
from app.ai.factory import build_llm
from app.ai.pricing import Prices, cost_report
from app.ai.types import TextPart
from tests.eval import manifest
from tests.eval.corpus import GOLD
from tests.eval.diagnostics.variants import GOLD_IDS, VARIANTS
from tests.factories import make_settings

RESULTS = Path(__file__).resolve().parents[1] / "results"
ARMS: tuple[tuple[str, str | None], ...] = (("default", None), ("none", "none"))
MAX_CALLS = 200
CONCURRENCY = 3
# gold `task` -> expected route; the SAME mapping the answer evaluation uses (see tests/eval/test_eval.py EXPECTED_TASK)
EXPECTED = {
    "qa": "qa",
    "figure": "qa",
    "diagram": "qa",
    "summarize": "summarize",
    "compare": "compare",
    "review": "document_review",
}


def items() -> list[dict[str, str]]:
    gold = {g.id: g for g in GOLD}
    out = [
        {
            "id": gid,
            "source": "gold",
            "instruction": gold[gid].instruction,
            "expected": EXPECTED[gold[gid].task],
        }
        for gid in GOLD_IDS
    ]
    out += [
        {"id": v.id, "source": "variant", "instruction": v.instruction, "expected": v.expected}
        for v in VARIANTS
    ]
    return out


async def _one(llm: Any, settings: Any, instruction: str, effort: str | None) -> dict[str, Any]:
    started = time.perf_counter()
    rec: dict[str, Any] = {}
    try:
        res = await llm.generate_structured(
            system=prompts.CLASSIFY_SYSTEM,
            parts=[TextPart(instruction)],
            schema=prompts.CLASSIFY_SCHEMA,
            schema_name=prompts.CLASSIFY_SCHEMA_NAME,
            max_output_tokens=settings.ai_classifier_max_output_tokens,
            reasoning_effort=effort,
        )
        task = res.data.get("task") if isinstance(res.data, dict) else None
        rec |= {
            "route": task if task in ROUTES else None,
            "invalid": task not in ROUTES,
            "failure": None,
            "in": res.usage.input_tokens,
            "out": res.usage.output_tokens,
        }
    except errors.ProviderError as exc:
        usage = exc.usage
        rec |= {
            "route": None,
            "invalid": False,
            "failure": exc.code,
            "in": getattr(usage, "input_tokens", None),
            "out": getattr(usage, "output_tokens", None),
        }
    rec["seconds"] = round(time.perf_counter() - started, 2)
    return rec


async def run(repeats: int) -> dict[str, Any]:
    todo = items()
    total = len(todo) * len(ARMS) * repeats
    if total > MAX_CALLS:  # before anything is built or sent
        raise SystemExit(f"{total} calls exceed the cap of {MAX_CALLS}; lower --repeats")
    settings = make_settings(ai_enabled=True)
    started = dt.datetime.now(dt.UTC)
    start_stage = {
        "started_utc": started.isoformat(),
        "git": manifest.git_state(),
        "config": manifest.safe_config(settings),
        "prompt_and_schema_hashes": manifest.prompts_and_schemas(),
        "variants_sha256": manifest.sha256_file(Path(__file__).with_name("variants.py")),
        "arms": {name: {"reasoning_effort_sent": effort} for name, effort in ARMS},
        "repeats": repeats,
        "instructions": len(todo),
        "planned_calls": total,
        "call_cap": MAX_CALLS,
        "classifier_output_budget": settings.ai_classifier_max_output_tokens,
    }
    llm = build_llm(settings, settings.ai_openai_api_key.get_secret_value())
    sem = asyncio.Semaphore(CONCURRENCY)
    rows: list[dict[str, Any]] = []

    async def job(it: dict[str, str], arm: str, effort: str | None, n: int) -> None:
        async with sem:
            rec = await _one(llm, settings, it["instruction"], effort)
        rows.append({"id": it["id"], "arm": arm, "attempt": n, "expected": it["expected"], **rec})

    work = []
    for n in range(1, repeats + 1):
        for i, it in enumerate(todo):
            order = ARMS if (n + i) % 2 == 0 else ARMS[::-1]  # alternate which arm goes first
            for arm, effort in order:
                work.append(job(it, arm, effort, n))
    try:
        await asyncio.gather(*work)
    finally:
        await llm.aclose()
    rows.sort(key=lambda r: (r["id"], r["arm"], r["attempt"]))
    return {
        "kind": "classifier-repeat",
        "evaluator": "classifier_repeat v1 (diagnostic, not acceptance)",
        "start_stage": start_stage,
        "completed_utc": dt.datetime.now(dt.UTC).isoformat(),
        "items": todo,
        "attempts": rows,
        "summary": summarise(todo, rows, settings),
    }


def _ratio(num: int, den: int) -> str:
    return f"{num}/{den}" if den else "n/a (0)"


def summarise(
    todo: list[dict[str, str]], rows: list[dict[str, Any]], settings: Any
) -> dict[str, Any]:
    arms = [a for a, _ in ARMS]
    by_item: dict[str, dict[str, Any]] = {}
    for it in todo:
        entry: dict[str, Any] = {"expected": it["expected"], "source": it["source"]}
        for arm in arms:
            mine = [r for r in rows if r["id"] == it["id"] and r["arm"] == arm]
            counts: dict[str, int] = {}
            for r in mine:
                key = r["route"] or f"failure:{r['failure'] or 'invalid'}"
                counts[key] = counts.get(key, 0) + 1
            entry[arm] = {
                "attempts": len(mine),
                "correct": sum(1 for r in mine if r["route"] == it["expected"]),
                "failures": sum(1 for r in mine if r["route"] is None),
                "routes": counts,
            }
        by_item[it["id"]] = entry
    by_route: dict[str, dict[str, Any]] = {}
    for route in sorted({it["expected"] for it in todo}):
        ids = {it["id"] for it in todo if it["expected"] == route}
        by_route[route] = {}
        for arm in arms:
            mine = [r for r in rows if r["id"] in ids and r["arm"] == arm]
            by_route[route][arm] = {
                "attempts": len(mine),
                "correct": sum(1 for r in mine if r["route"] == route),
                "failures": sum(1 for r in mine if r["route"] is None),
            }
    per_arm: dict[str, Any] = {}
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        secs = sorted(r["seconds"] for r in mine)
        t_in = sum(r["in"] or 0 for r in mine)
        t_out = sum(r["out"] or 0 for r in mine)
        per_arm[arm] = {
            "attempts": len(mine),
            "input_tokens": t_in,
            "output_tokens_incl_hidden_reasoning": t_out,
            "mean_output_tokens": round(t_out / len(mine), 1) if mine else None,
            "median_seconds": secs[len(secs) // 2] if secs else None,
            "max_seconds": secs[-1] if secs else None,
            "cost": cost_report(Prices.of(settings), llm_in=t_in, llm_out=t_out, embed_tokens=0),
        }
    verdict = decide(by_item, by_route)
    return {"per_item": by_item, "per_route": by_route, "per_arm": per_arm, "rule": verdict}


def decide(by_item: dict[str, Any], by_route: dict[str, Any]) -> dict[str, Any]:
    """Rules 2-4 of the decision rule in the module docstring (rule 1 needs the full evaluation)."""
    dr = by_item["diagram-release"]["none"]
    r2 = dr["correct"] == dr["attempts"] and dr["attempts"] > 0
    r3 = all(
        v["none"]["correct"] >= v["default"]["correct"] and v["none"]["failures"] == 0
        for v in by_route.values()
    )
    bad = [
        i
        for i, v in by_item.items()
        if v["source"] == "gold"
        and v["default"]["correct"] == v["default"]["attempts"]
        and v["none"]["correct"] < v["none"]["attempts"]
    ]
    return {
        "rule2_diagram_release_all_correct_under_none": r2,
        "rule3_no_route_worse_and_no_failures": r3,
        "rule4_gold_items_worse_under_none": bad,
        "rule4_ok": not bad,
        "rules_2_to_4_hold": r2 and r3 and not bad,
    }


def render(result: dict[str, Any]) -> str:
    s = result["summary"]
    st = result["start_stage"]
    lines = [
        "# Classifier repeat check (diagnostic, not acceptance)",
        "",
        f"Model `{st['config'].get('ai_llm_model')}`; classifier output budget {st['classifier_output_budget']};"
        f" {st['repeats']} attempts per instruction per arm; {st['planned_calls']} calls (cap {st['call_cap']}).",
        "Arms: `default` = effort not sent; `none` = effort `none`. Correct means: equals the route written down beforehand.",
        "",
        "## By route (correct / attempts; failures)",
        "",
        "| Expected route | default | none |",
        "|---|---|---|",
    ]
    for route, v in s["per_route"].items():
        d, n = v["default"], v["none"]
        lines.append(
            f"| {route} | {_ratio(d['correct'], d['attempts'])}; {d['failures']} failed "
            f"| {_ratio(n['correct'], n['attempts'])}; {n['failures']} failed |"
        )
    lines += [
        "",
        "## By instruction",
        "",
        "| Instruction | Source | Expected | default (routes returned) | none (routes returned) |",
        "|---|---|---|---|---|",
    ]
    for iid, v in s["per_item"].items():
        d, n = v["default"], v["none"]
        lines.append(
            f"| {iid} | {v['source']} | {v['expected']} | {_ratio(d['correct'], d['attempts'])} {d['routes']} "
            f"| {_ratio(n['correct'], n['attempts'])} {n['routes']} |"
        )
    lines += [
        "",
        "## Tokens, latency, cost",
        "",
        "| Arm | Attempts | Input tokens | Output tokens (incl. hidden reasoning) | Mean output | Median s | Max s | Cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm, v in s["per_arm"].items():
        c = v["cost"]
        cost = (
            "unknown (no prices configured)"
            if c["cost_status"] == "unpriced"
            else f"{c['cost_status']}: {c['est_cost_micros']} micros"
        )
        lines.append(
            f"| {arm} | {v['attempts']} | {v['input_tokens']} | {v['output_tokens_incl_hidden_reasoning']} "
            f"| {v['mean_output_tokens']} | {v['median_seconds']} | {v['max_seconds']} | {cost} |"
        )
    r = s["rule"]
    lines += [
        "",
        "## Decision rule 2-4 (rule 1 is the full evaluation)",
        "",
        f"* 2. diagram-release correct on every attempt under `none`: **{r['rule2_diagram_release_all_correct_under_none']}**",
        f"* 3. no route worse under `none`, and no failures: **{r['rule3_no_route_worse_and_no_failures']}**",
        f"* 4. gold items right every time by default but wrong at least once under `none`: **{r['rule4_gold_items_worse_under_none'] or 'none'}**",
        f"* Rules 2-4 hold: **{r['rules_2_to_4_hold']}**",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    result = asyncio.run(run(args.repeats))
    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"classifier-repeat-{stamp}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    report = render(result)
    (RESULTS / f"classifier-repeat-{stamp}.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
