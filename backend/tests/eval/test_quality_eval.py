"""The task-quality evaluation (dataset `quality-v1`, tests/eval/quality.py). Not part of CI (see conftest.py).

    RUN_EVAL=1 QUALITY_SPLIT=dev     uv run --env-file ../.env python -m pytest tests/eval/test_quality_eval.py -s
    RUN_EVAL=1 QUALITY_SPLIT=heldout EVAL_PROVIDER=real ...        the held-out split, run once, after the dev results are in
    RUN_EVAL=1 QUALITY_SPLIT=all     EVAL_PROVIDER=real ...        both, for a baseline run of an older commit

Runs every item through the REAL request pipeline (upload, analysis, retrieval or capture, generation, validation, publication),
scores it with tests/eval/quality_score.py, and writes a report and a record file next to the others. The acceptance criteria are in
the docstring of tests/eval/quality.py and were fixed before the first run.

In `stub` mode the model is a scripted stand-in that knows nothing about these tasks: the run proves the harness works and its
numbers are NOT a measurement of anything. Only `EVAL_PROVIDER=real` measures a model, and then every number belongs to that model, on
that day, on a synthetic corpus.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.ai.edit.check import available
from tests.ai_request_helpers import FakeCollab
from tests.eval import evidence, manifest, quality
from tests.eval.checks import placeholder_hits
from tests.eval.metrics import METRIC_VERSIONS, failed_calls, mean, percentile
from tests.eval.quality import QItem
from tests.eval.quality_score import Outputs, flag_recall, score_item, sentence_text
from tests.eval.test_eval import (
    MODE,
    build,
    citation_origins,
    cost_lines,
    failure_lines,
    ledger_of,
    manifest_of,
    origin_resolves,
)
from tests.helpers import rows

RESULTS = Path(__file__).parent / "results"
SPLIT = os.environ.get("QUALITY_SPLIT", "all")

pytestmark = pytest.mark.skipif(
    MODE not in ("stub", "real"), reason="EVAL_PROVIDER must be stub or real"
)


def collab_for(world, item: QItem) -> FakeCollab | None:
    if not item.document:
        return None
    return FakeCollab(
        world.document["id"],
        world.cfg.internal_api_secret.get_secret_value(),
        items=list(quality.DOCUMENTS[item.document]),
    )


def fields_for(world, item: QItem, fc: FakeCollab | None) -> dict[str, Any]:
    if item.kind == "draft":
        return {"operation": "draft", "document_id": None, "task_hint": "ask"}
    if item.kind == "edit":
        assert fc is not None
        f: dict[str, Any] = {
            "operation": "edit",
            "document_id": world.document["id"],
            "include_document_content": True,
            "document_scope": "edit",
            "task_hint": "ask",
        }
        if item.blocks:
            f["selected_block_ids"] = fc.block_ids(*item.blocks)
            f["selection_hash"] = fc.hash_for(*item.blocks)
        if item.insert_at:
            f["insert_at"] = item.insert_at
        return f
    f = {"task_hint": item.task_hint}
    if item.include_document:
        f |= {"include_document_content": True, "document_scope": "whole"}
    return f


def answer_text(blocks: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for b in blocks:
        if b["type"] == "paragraph":
            out.append(b["text"])
        elif b["type"] == "bullet_list":
            out += [it["text"] for it in b["items"]]
    return "\n".join(out)


def diagram_source(node: dict[str, Any]) -> str:
    return "".join(c.get("text", "") for c in node.get("content", []))


def is_note(node: dict[str, Any]) -> bool:
    try:
        from app.ai.edit.schema import ASSUMPTION_NOTE
    except (
        ImportError
    ):  # a build from before the note existed (the baseline run): it never wrote one
        ASSUMPTION_NOTE = "Assumed, not stated in the source: "
    return node.get("type") == "paragraph" and sentence_text([node]).startswith(
        ASSUMPTION_NOTE.strip()
    )


def proposal_outputs(item: QItem, preview: dict[str, Any], fc: FakeCollab | None) -> Outputs:
    out = Outputs(published=True)
    texts: list[str] = []
    if preview.get("kind") == "draft":
        for n in (preview.get("document") or {}).get("content", []):
            if n.get("type") == "diagram":
                out.diagrams.append(diagram_source(n))
            elif is_note(n):
                out.notes.append(sentence_text([n]))
            else:
                texts.append(sentence_text([n]))
        for per_block in preview.get("block_assumptions") or []:
            out.assumptions += per_block
        out.text = "\n".join(t for t in texts if t)
        return out
    index_of = {b.id: i for i, b in enumerate(fc.blocks)} if fc else {}
    for change in preview.get("changes", []):
        out.assumptions += change.get("assumptions") or []
        for n in change.get("after", []):
            if n.get("type") == "diagram":
                out.diagrams.append(diagram_source(n))
            elif is_note(n):
                out.notes.append(sentence_text([n]))
            else:
                texts.append(sentence_text([n]))
        if (
            change.get("kind") in ("replace_block_text", "replace_range")
            and change.get("block_id") in index_of
        ):
            idx = index_of[change["block_id"]]
            out.changed[idx] = (
                sentence_text(change.get("before", [])),
                sentence_text(change.get("after", [])),
            )
            out.review_flags[idx] = change.get("review_flags") or []
    out.text = "\n".join(t for t in texts if t)
    return out


async def run_item(world, item: QItem, uploaded: dict[str, dict]) -> dict[str, Any]:
    fc = collab_for(world, item)
    started = time.perf_counter()
    r = await world.send(
        world.owner,
        item.instruction,
        sources=[uploaded[n] for n in item.sources],
        **fields_for(world, item, fc),
    )
    record: dict[str, Any] = {
        "id": item.id,
        "split": item.split,
        "finding": item.finding,
        "kind": item.kind,
        "instruction": item.instruction,
        "sources": list(item.sources),
        "document": item.document,
    }
    if r.status_code != 201:
        record |= {
            "outcome": f"rejected:{r.json().get('error', {}).get('code')}",
            "published": False,
        }
        record["score"] = score_item(item, Outputs(published=False))
        return record
    rid = r.json()["id"]
    record["request_id"] = rid
    record["outcomes"] = await world.drain(collab=fc.transport() if fc else None)
    record["latency_s"] = time.perf_counter() - started
    record["ledger"] = await ledger_of(world, rid)
    result_r = await world.request(world.owner, rid, "/result")
    if result_r.status_code != 200:
        state = (await world.request(world.owner, rid)).json()
        record |= {"published": False, "outcome": f"{state.get('state')}:{state.get('error_code')}"}
        record["score"] = score_item(item, Outputs(published=False))
        return record
    result = result_r.json()
    data = await manifest_of(world, rid)
    coverage = result.get("coverage") or {}
    stats = (result.get("answer") or {}).get("stats", {})
    validation = coverage.get("validation") or {}
    citations = result["citations"]
    origins = await citation_origins(world, rid, citations)
    [message] = await rows(
        world.sm, "SELECT body FROM messages WHERE id = :i", i=uuid.UUID(result["reply_message_id"])
    )
    if item.kind == "answer":
        blocks = result["answer"]["blocks"]
        diagrams = [b for b in blocks if b["type"] == "diagram"]
        out = Outputs(
            published=True,
            text=answer_text(blocks),
            diagrams=[b["mermaid"] for b in diagrams],
            assumptions=[a for b in diagrams for a in (b.get("assumptions") or [])],
            gaps=result["answer"]["evidence_gaps"],
            renders=(all(b.get("render_status") == "ok" for b in diagrams) if diagrams else None),
        )
        record["output_raw"] = result["answer"]
    else:
        [prop] = await rows(
            world.sm, "SELECT * FROM ai_proposals WHERE request_id = :i", i=uuid.UUID(rid)
        )
        out = proposal_outputs(item, prop["preview"], fc)
        out.gaps = result["answer"].get("evidence_gaps", [])
        out.renders = (
            True if out.diagrams else None
        )  # a proposal only publishes when its diagrams parse
        record["output_raw"] = {
            "summary": result["answer"].get("summary"),
            "preview": prop["preview"],
        }
    out.chat_text = message["body"]
    out.citations = citations
    out.stats = stats
    out.notices = coverage.get("notices") or []
    out.sent_document = bool(data.get("document"))
    out.derived_resolves = origin_resolves(origins, citations)
    out.first_try_valid = validation.get("first_try_valid")
    out.repaired = validation.get("repaired")
    record |= {
        "published": True,
        "outcome": "published",
        "task_type": result["task_type"],
        "routing": coverage.get("routing"),
        "score": score_item(item, out),
        "chat_text": out.chat_text,
        "gaps": out.gaps,
        "citations": citations,
        "citation_origins": origins,
        "coverage": coverage,
        "stats": stats,
        "placeholder_hits": placeholder_hits(out.text),
    }
    return record


def fmt(value: float | None, pct: bool = False) -> str:
    return "n/a" if value is None else (f"{value * 100:.0f}%" if pct else f"{value:.2f}")


def build_report(mode: str, records: list[dict[str, Any]], run: dict | None) -> str:
    stub = mode == "stub"
    lines = [
        f"# Quality evaluation ({'local stand-in model: NOT a measurement' if stub else 'REAL provider'})",
        "",
        f"Dataset `{quality.DATASET_VERSION}` (fingerprint {quality.dataset_fingerprint()[:12]}), split `{SPLIT}`. Run: "
        f"{dt.datetime.now(dt.UTC).strftime('%Y-%m-%d %H:%M UTC')}. Items: {len(records)}. "
        f"Criteria: tests/eval/quality.py (declared before the first run).",
        "",
        "Three kinds of result, kept apart: **structural** (valid, rendered, cited), **semantic** (deterministic content checks against a "
        "written rubric), **finding** (this item against its finding's criterion). None is a human review or a judge.",
        "",
    ]
    for sp in ("dev", "heldout"):
        part = [r for r in records if r["split"] == sp]
        if not part:
            continue
        lines += [
            f"## Split `{sp}`",
            "",
            "| Finding | Items | Published | Finding met | Items |",
            "|---|---|---|---|---|",
        ]
        for f in sorted({r["finding"] for r in part}):
            fs = [r for r in part if r["finding"] == f]
            met = [r for r in fs if r["score"]["finding_met"]]
            lines.append(
                f"| {f} | {len(fs)} | {sum(1 for r in fs if r.get('published'))}/{len(fs)} | {len(met)}/{len(fs)} | "
                f"{', '.join(r['id'] + (' (met)' if r['score']['finding_met'] else ' (NOT met)') for r in fs)} |"
            )
        fr = flag_recall([r["score"] for r in part if r["finding"] == "F5"])
        lines += [
            "",
            f"F5 flag recall (rewritten sentences that lost a fact of their rubric and were marked by the app's review flag): "
            f"{fr['flagged']}/{fr['lost']}"
            + ("" if fr["lost"] else " (nothing was lost, so nothing to flag)"),
            "",
        ]
    lines += [
        "## Per item",
        "",
        "| Item | Split | Finding | Published | Finding met | Why (checks) |",
        "|---|---|---|---|---|---|",
    ]
    for r in records:
        s = r["score"]
        lines.append(
            f"| {r['id']} | {r['split']} | {r['finding']} | {'yes' if r.get('published') else 'no: ' + str(r.get('outcome'))} | "
            f"{'yes' if s['finding_met'] else 'NO'} | {'; '.join(s['explain'])[:300]} |"
        )
    led = [r["ledger"] for r in records if "ledger" in r]
    lat = [r["latency_s"] for r in records if "latency_s" in r]
    lines += [
        "",
        f"Latency p50 / p95 per request (s): {fmt(percentile(lat, 50))} / {fmt(percentile(lat, 95))}. Model calls per request (mean): "
        f"{fmt(mean([x['calls'] for x in led]))}. Input / output tokens per request (mean): "
        f"{fmt(mean([x['input_tokens'] for x in led]))} / {fmt(mean([x['output_tokens'] for x in led]))}.",
        "",
        "## Provider failures (run-wide usage ledger)",
        "",
        *failure_lines(run),
        "",
        "## Cost",
        "",
        *cost_lines(run),
        "",
        "## How to read this",
        "",
        "* One sample per item per run; a single item flipping is noise. Dev results show the fixes work on what they were built from; only "
        "held-out results can show they generalize.",
        "* `concepts` is a recall check on named points, `figures` a check on numbers and times, `placeholder` a diagnostic for one known failure, "
        "`order disclosed` a check on what the output SAID. They cannot tell whether a statement is true or an output useful: that is read by hand "
        "(agent assessment) and by a human reviewer.",
        "* Metric versions: " + ", ".join(f"{k} = {v}" for k, v in METRIC_VERSIONS.items()) + ".",
    ]
    return "\n".join(lines) + "\n"


async def test_evaluate_quality(make_actor, sessionmaker, tmp_path, redis_prefix):
    from tests.factories import make_settings

    if not available(make_settings()):
        pytest.skip("run `npm run build` first")
    items = quality.split(SPLIT)
    world = await build(make_actor, sessionmaker, tmp_path, redis_prefix, ai_proposals_enabled=True)
    needed = sorted({s for i in items for s in i.sources})
    sources = quality.all_sources()
    run_manifest = manifest.begin(
        kind="quality",
        mode=MODE,
        settings=world.cfg,
        expected_items=len(items),
        dataset={
            "dataset_version": quality.DATASET_VERSION,
            "dataset_fingerprint": quality.dataset_fingerprint(),
            "split": SPLIT,
            "items": [i.id for i in items],
            "quality_py_sha256": manifest.sha256_file(Path(quality.__file__)),
            "sources_sha256": {n: manifest.sha256_bytes(sources[n]) for n in needed},
            "fixture_identity": {n: evidence.fixture_identity(n, sources[n]) for n in needed},
            "acceptance_criteria": quality.criteria(),
            "metric_versions": METRIC_VERSIONS,
        },
    )
    started = dt.datetime.fromisoformat(run_manifest["started_utc"])
    if world.stub is not None:
        world.stub.classify_as = "document_review"  # whatever: a stub run measures nothing
    uploaded: dict[str, dict] = {}
    for name in needed:
        uploaded[name] = await world.upload(world.owner, name, sources[name])
        assert (await world.analyze(world.owner, uploaded[name])).status_code == 202
    assert set(await world.run_worker(limit=80)) == {"succeeded"}

    records = [await run_item(world, item, uploaded) for item in items]
    ledger = await manifest.ledger_summary(world.sm, started)
    run = {"failures": failed_calls(ledger), "cost": manifest.cost_summary(world.cfg, ledger)}
    report = build_report(MODE, records, run)
    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"quality-{MODE}-{SPLIT}-{stamp}.json").write_text(
        json.dumps(
            {"mode": MODE, "split": SPLIT, "run": run, "records": records}, indent=2, default=str
        ),
        encoding="utf-8",
    )
    (RESULTS / f"quality-{MODE}-{SPLIT}-results.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    run_manifest = await manifest.finish(
        run_manifest,
        settings=world.cfg,
        sessionmaker=world.sm,
        records=records,
        results={
            "items": len(records),
            "published": sum(1 for r in records if r.get("published")),
            "finding_met": {r["id"]: r["score"]["finding_met"] for r in records},
        },
    )
    manifest.write(RESULTS / f"quality-{MODE}-{SPLIT}-{stamp}.manifest.json", run_manifest)
    assert any(r.get("published") for r in records), "no quality item published anything"
