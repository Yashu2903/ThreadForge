"""The phase 2 evaluation gate: edit and draft generation. Not part of CI (see conftest.py).

    RUN_EVAL=1 uv run pytest tests/eval/test_proposal_eval.py -s                      # local stand-in model
    RUN_EVAL=1 EVAL_PROVIDER=real uv run pytest tests/eval/test_proposal_eval.py -s   # the configured model

THE GATE (the owner's phase 2 brief, section 0): after checkpoint 2, run the gold set with the configured
model and report
  * the schema-valid rate WITHOUT repair and WITH one repair,
  * the rate of proposals whose base hashes and anchors resolve,
  * citation validity,
  * a sample of previews.
If the schema-valid rate with one repair is below 90 % on the gold set, STOP and report to the owner instead
of building the apply path.

THE TWO MODES, and why the verdict differs:
  stub  the model is a scripted stand-in: some replies are well-formed and some carry the defects a real model
        makes (a wrong label, a hostile diagram, text that is not JSON). Everything else is real: capture,
        target resolution, validation, the Node engine, repair, publication. The rates therefore show the
        HARNESS and the repair path work; they are NOT measurements of any model, so the verdict is
        UNMEASURED and the gate is not passed.
  real  the configured provider answers. The verdict is PASS or FAIL against the 90 % line. This mode has NOT
        been run (no credentials were available); the first real run is the owner's to make.
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

from app.ai.edit.check import available, run_check
from tests.ai_request_helpers import FakeCollab
from tests.analysis_helpers import World
from tests.eval import corpus
from tests.eval.metrics import mean, percentile, share
from tests.eval.proposals import GOLD, PGold
from tests.eval.test_eval import MODE, build, ledger_of
from tests.helpers import rows

RESULTS = Path(__file__).parent / "results"
GATE = 0.90

pytestmark = pytest.mark.skipif(
    MODE not in ("stub", "real"), reason="EVAL_PROVIDER must be stub or real"
)


def collab_for(world: World) -> FakeCollab:
    return FakeCollab(
        world.document["id"],
        world.cfg.internal_api_secret.get_secret_value(),
        items=list(corpus.DOCUMENT_ITEMS),
    )


def fields_for(world: World, item: PGold, fc: FakeCollab) -> dict[str, Any]:
    import hashlib

    from app.ai.edit.units import block_units, utf16_slice

    if item.kind == "draft":
        return {"operation": "draft", "document_id": None, "task_hint": "ask"}
    fields: dict[str, Any] = {
        "operation": "edit",
        "document_id": world.document["id"],
        "include_document_content": True,
        "document_scope": "edit",
        "task_hint": "ask",
    }
    if item.blocks:
        fields["selected_block_ids"] = fc.block_ids(*item.blocks)
        fields["selection_hash"] = fc.hash_for(*item.blocks)
    if item.range_:
        index, start, end = item.range_
        node = fc.content["content"][index]
        text = utf16_slice(block_units(node), start, end)
        fields["selected_ranges"] = [
            {
                "block_id": fc.block_ids(index)[0],
                "start": start,
                "end": end,
                "text_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        ]
    if item.insert_at:
        fields["insert_at"] = item.insert_at
    return fields


async def resolves(world: World, fc: FakeCollab, proposal: Any) -> bool:
    """Do the proposal's base hashes and anchors still resolve against the captured document, and does
    replaying its operations against that document produce a valid result? (The same check the apply path
    will make against the LIVE document.)"""
    if proposal["kind"] == "draft":
        return True
    by_id = {b.id: b for b in fc.blocks}
    for block_id, digest in (proposal["base_hashes"] or {}).items():
        if block_id not in by_id or by_id[block_id].hash != digest:
            return False
    scope = proposal["scope"]
    for anchor in scope.get("anchors", []):
        if anchor != "start" and anchor not in by_id:
            return False
    outcome = await run_check(
        world.cfg,
        {
            "mode": "edit",
            "content": fc.content,
            "operations": proposal["operations"],
            "scope": {
                "whole_ids": scope.get("whole_ids", []),
                "ranges": scope.get("ranges", []),
                "anchors": scope.get("anchors", []),
            },
        },
    )
    return outcome.ok


async def run_item(world: World, item: PGold, uploaded: dict[str, dict]) -> dict[str, Any]:
    fc = collab_for(world)
    if world.stub is not None:
        world.stub.edit_script = list(item.script) if item.kind == "edit" else []
        world.stub.draft_script = list(item.script) if item.kind == "draft" else []
    fields = fields_for(world, item, fc)
    started = time.perf_counter()
    r = await world.send(
        world.owner, item.instruction, sources=[uploaded[n] for n in item.sources], **fields
    )
    record: dict[str, Any] = {"id": item.id, "kind": item.kind, "defect": item.defect}
    if r.status_code != 201:
        return record | {
            "outcome": f"rejected:{r.json().get('error', {}).get('code')}",
            "published": False,
        }
    rid = r.json()["id"]
    await world.drain(collab=fc.transport())
    record["latency_s"] = time.perf_counter() - started
    [req] = await rows(world.sm, "SELECT * FROM ai_requests WHERE id = :i", i=uuid.UUID(rid))
    record["ledger"] = await ledger_of(world, rid)
    props = await rows(
        world.sm, "SELECT * FROM ai_proposals WHERE request_id = :i", i=uuid.UUID(rid)
    )
    if not props:
        detail = req["error_detail"] or {}
        metrics = detail.get("metrics") or {}
        return record | {
            "published": False,
            "outcome": f"{req['state']}:{req['error_code']}",
            "first_try_valid": bool(metrics.get("first_try_valid", False)),
            "repaired": bool(metrics.get("repaired", False)),
            "problems": metrics.get("first_problems") or [],
        }
    proposal = props[0]
    result = (await world.request(world.owner, rid, "/result")).json()
    metrics = result["coverage"].get("validation") or {}
    stats = result["answer"].get("stats", {})
    claims = stats.get("claims", 0)
    return record | {
        "published": True,
        "outcome": "published",
        "first_try_valid": bool(metrics.get("first_try_valid")),
        "repaired": bool(metrics.get("repaired")),
        "problems": metrics.get("first_problems") or [],
        "resolves": await resolves(world, fc, proposal),
        "citation_valid": (None if claims == 0 else 1 - stats.get("unsupported", 0) / claims),
        "invalid_citation_refs": stats.get("invalid_citations", 0),
        "operations": result["answer"]["operation_count"],
        "preview": proposal["preview"],
        "instruction": item.instruction,
    }


def sample(record: dict[str, Any]) -> list[str]:
    """A readable preview of one proposal, for the report."""
    preview = record["preview"]
    lines = [f"**{record['id']}**: {record['instruction']}"]
    if preview.get("kind") == "draft":
        types = [n["type"] for n in (preview.get("document") or {}).get("content", [])]
        lines.append(f'  draft "{preview.get("title")}": blocks {", ".join(types)}')
        return lines
    for c in preview.get("changes", []):
        before = " / ".join(_text(n) for n in c["before"]) or "(nothing)"
        after = " / ".join(_text(n) or f"[{n.get('type')}]" for n in c["after"]) or "(deleted)"
        lines.append(f'  {c["kind"]}: "{before}" -> "{after}"')
    return lines


def _text(node: dict[str, Any]) -> str:
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_text(c) for c in node.get("content", []))


def fmt(value: float | None, digits: int = 2) -> str:
    return (
        "n/a" if value is None else f"{value * 100:.0f}%" if digits == 0 else f"{value:.{digits}f}"
    )


def build_report(mode: str, records: list[dict[str, Any]]) -> tuple[str, float | None]:
    stub = mode == "stub"
    first = share([r.get("first_try_valid", False) for r in records])
    final = share([bool(r.get("published")) for r in records])
    pub = [r for r in records if r.get("published")]
    resolved = share([r["resolves"] for r in pub])
    cites = mean([r["citation_valid"] for r in pub if r.get("citation_valid") is not None])
    repaired_ok = [r for r in pub if r["repaired"]]
    lat = [r["latency_s"] for r in records if "latency_s" in r]
    verdict = (
        "UNMEASURED (stub). The numbers below come from a scripted stand-in model, so they cannot pass or "
        "fail the gate. Run the harness with the configured model."
        if stub
        else (
            f"PASS: the schema-valid rate with one repair is {fmt(final, 0)} (line: 90%)."
            if final is not None and final >= GATE
            else f"FAIL: the schema-valid rate with one repair is {fmt(final, 0)}, below the 90% line. "
            "STOP: do not build the apply path; report to the owner."
        )
    )
    basis = "stub: scripted replies, NOT a model measurement" if stub else "measured"
    lines = [
        f"# Proposal evaluation gate ({'local stand-in model' if stub else 'REAL provider'})",
        "",
        f"Run: {dt.datetime.now(dt.UTC).strftime('%Y-%m-%d %H:%M UTC')}. Gold items: {len(records)} "
        f"({sum(1 for r in records if r['kind'] == 'edit')} edits, "
        f"{sum(1 for r in records if r['kind'] == 'draft')} drafts).",
        "",
        f"**Gate verdict: {verdict}**",
        "",
        "| Metric | Value | Basis |",
        "|---|---|---|",
        f"| Schema-valid WITHOUT repair | {fmt(first, 0)} ({sum(1 for r in records if r.get('first_try_valid'))}/{len(records)}) | {basis} |",
        f"| Schema-valid WITH one repair (published) | {fmt(final, 0)} ({len(pub)}/{len(records)}) | {basis} |",
        f"| Proposals whose base hashes, anchors and replay resolve | {fmt(resolved, 0)} ({sum(1 for r in pub if r['resolves'])}/{len(pub)}) | measured: the real capture, hashes and the real engine |",
        f"| Citation validity (supported claims / claims, mean per proposal) | {fmt(cites, 0)} | {basis} |",
        f"| Repairs that fixed the first reply | {len(repaired_ok)} of {sum(1 for r in records if r.get('repaired'))} repaired | {basis} |",
        f"| Latency p50 / p95 per request (s) | {fmt(percentile(lat, 50))} / {fmt(percentile(lat, 95))} | "
        f"{'stub: pipeline time only' if stub else 'measured, end to end'} |",
        f"| Model calls per request (mean) | {fmt(mean([r['ledger']['answer_calls'] for r in records if 'ledger' in r]), 1)} | measured (usage ledger) |",
        "",
        "## Per item",
        "",
        "| Item | Kind | First try valid | Repaired | Outcome | First reply's problems |",
        "|---|---|---|---|---|---|",
    ]
    for r in records:
        lines.append(
            f"| {r['id']} | {r['kind']} | {'yes' if r.get('first_try_valid') else 'no'} | "
            f"{'yes' if r.get('repaired') else 'no'} | {r.get('outcome')} | {', '.join(r.get('problems') or []) or '-'} |"
        )
    lines += ["", "## A sample of previews", ""]
    for r in pub[:4]:
        lines += sample(r) + [""]
    lines += [
        "## How to read this",
        "",
        '* "Schema-valid" means the reply became typed operations (or a draft) that the shared document engine '
        "accepted against the captured document: labels, scope, anchors, ranges, links, diagrams, limits and "
        "the full-document schema check. Without repair = the first reply; with one repair = the first or the "
        "reply to one repair request.",
        "* The stub items with a `defect` get a bad first reply on purpose, to show the repair path; the stub "
        "result is therefore a test of the harness and of the repair loop, not of a model.",
        f"* Small gold set ({len(records)} items): treat differences of a few points as noise.",
    ]
    return "\n".join(lines) + "\n", final


async def test_evaluate_proposals(make_actor, sessionmaker, tmp_path, redis_prefix):
    from tests.factories import make_settings

    if not available(make_settings()):
        pytest.skip("run `npm run build` first")
    world = await build(make_actor, sessionmaker, tmp_path, redis_prefix, ai_proposals_enabled=True)
    if world.stub is not None:
        world.stub.classify_as = "qa"

    uploaded: dict[str, dict] = {}
    for name in ("release.md", "depots.pdf"):
        uploaded[name] = await world.upload(world.owner, name, corpus.sources()[name])
        assert (await world.analyze(world.owner, uploaded[name])).status_code == 202
    assert set(await world.run_worker(limit=60)) == {"succeeded"}

    records = [await run_item(world, item, uploaded) for item in GOLD]
    report, final = build_report(MODE, records)
    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"proposals-{MODE}-{stamp}.json").write_text(
        json.dumps({"mode": MODE, "records": records}, indent=2, default=str), encoding="utf-8"
    )
    (RESULTS / f"proposals-{MODE}-results.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    assert any(r.get("published") for r in records), "no gold item produced a proposal"
    if MODE == "real":
        assert final is not None and final >= GATE, "THE GATE FAILED: stop and report to the owner"
    assert os.environ.get("EVAL_PROVIDER", "stub") in ("stub", "real")
