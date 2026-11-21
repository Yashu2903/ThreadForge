"""The evaluation harness: runs the gold set through the REAL upload, analysis, retrieval, request and
answer code and writes a results report. Not part of CI (see conftest.py).

    RUN_EVAL=1 uv run pytest tests/eval -s
    RUN_EVAL=1 EVAL_PROVIDER=real uv run pytest tests/eval -s     (needs real keys in the environment)

Two modes, and the difference matters when you read the numbers:

* stub (default): the providers are the local HTTP stand-ins used by the tests. Everything else is real:
  real files, the sandboxed extractor, real Tesseract, real chunking, PostgreSQL full-text search and
  pgvector, ranking, validation, publication. Some numbers are therefore real measurements (OCR
  accuracy, lexical retrieval, the share of requests that complete) and some are NOT measurements of
  any model (anything a model decides: citations, diagrams, schema compliance, tokens, cost, network
  latency). The report labels each one.
* real: no stub; the configured providers are called. Every number is then a measurement of those
  models. First run: 2026-11-19 (see docs/ai.md 22.2 and docs/evidence/ai-eval/). This evaluator has no
  acceptance threshold: its only assertions are that analysis succeeded and that something published.
  The test code ignores .env files, so load the keys with `uv run --env-file ../.env`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from app.ai.notice import NOTICE_VERSION
from app.storage import Storage
from tests.ai_request_helpers import FakeCollab
from tests.analysis_helpers import (
    World,
    answer,
    block,
    build_world,
    diagram,
)
from tests.doc_helpers import room_with_doc
from tests.eval import corpus, evidence, manifest
from tests.eval.corpus import GOLD, Gold
from tests.eval.metrics import (
    METRIC_VERSIONS,
    character_accuracy,
    contains,
    failed_calls,
    mean,
    percentile,
    recall_at_k,
    routing_metrics,
    share,
)
from tests.factories import make_settings
from tests.helpers import rows

MODE = os.environ.get("EVAL_PROVIDER", "stub")
RESULTS = Path(__file__).parent / "results"
EXPECTED_TASK = {
    "qa": "qa",
    "figure": "qa",
    "diagram": "qa",
    "summarize": "summarize",
    "compare": "compare",
    "review": "document_review",
}
KS = (1, 3, 5)


class EvalWorld(World):
    """A World that can run without the stub (real providers)."""

    transports: Any = None

    async def drain(self, *, rounds: int = 60, collab=None) -> list[str]:  # type: ignore[override]
        if self.stub is not None:
            return await super().drain(rounds=rounds, collab=collab)
        from app.ai.analysis.pipeline import process_next
        from app.ai.answer.execute import process_next_request
        from app.services import ai_requests

        out: list[str] = []
        for _ in range(rounds):
            progressed = bool(
                await ai_requests.advance_waiting(self.sm, self.cfg)
                + await ai_requests.expire_stale(self.sm, self.cfg)
            )
            res = await process_next(self.sm, self.cfg, self.store, transports=None)
            if res is not None:
                out.append("analysis:" + res)
                progressed = True
            res = await process_next_request(
                self.sm, self.cfg, self.store, transports=None, collab_transport=collab
            )
            if res is not None:
                out.append("request:" + res)
                progressed = True
            if not progressed:
                break
        return out

    async def run_worker(self, *, limit: int = 20) -> list[str]:
        if self.stub is not None:
            return await super().run_worker(limit=limit)
        from app.ai.analysis.pipeline import process_next

        outcomes = []
        for _ in range(limit):
            outcome = await process_next(self.sm, self.cfg, self.store, transports=None)
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes


async def build(make_actor, sessionmaker, tmp_path, redis_prefix, **extra) -> EvalWorld:
    if MODE == "stub":
        base = await build_world(
            make_actor,
            sessionmaker,
            tmp_path,
            redis_prefix,
            ai_max_active_requests_per_user=50,
            **extra,
        )
        return EvalWorld(
            base.owner, base.room_id, base.document, base.sm, base.store, base.cfg, base.stub
        )
    # real providers: keys, models, prices and dimensions come from the environment (see .env.example)
    cfg = make_settings(
        ai_enabled=True,
        redis_channel_prefix=redis_prefix,
        storage_path=str(tmp_path / "storage"),
        ai_max_active_requests_per_user=50,
        **extra,
    )
    owner = await make_actor("Owner", app_settings=cfg)
    room_id, document = await room_with_doc(owner, sessionmaker)
    r = await owner.post("/ai/consent", json={"notice_version": NOTICE_VERSION})
    assert r.status_code == 204, r.text
    return EvalWorld(
        owner, room_id, document, sessionmaker, Storage(tmp_path / "storage"), cfg, None
    )


def simulated_model(current: dict[str, Gold | None]):
    """The stub's stand-in for a competent model: cites the two evidence labels that share the most words
    with the question, and adds a diagram when the request is for one. It measures nothing about any real
    model; it only lets the validation and publication code run on a well-behaved answer."""

    def words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3}

    def respond(prompt):
        labels = prompt.labels
        if not labels:
            return answer(block("There is no evidence for this."), gaps=("no evidence",))
        item = current["item"]
        asked = words(item.instruction if item else "")
        ranked = sorted(
            labels, key=lambda lb: (-len(asked & words(prompt.items.get(lb, ""))), labels.index(lb))
        )
        blocks = [block("The evidence answers the request.", *ranked[:2])]
        if item is not None and item.expect_diagram:
            blocks.append(diagram("flowchart TD\n  A[Start] --> B[Finish]", ranked[0]))
        return answer(*blocks)

    return respond


async def ledger_of(world: World, request_id: str) -> dict[str, Any]:
    import uuid

    got = await rows(
        world.sm,
        "SELECT operation, count(*) AS calls, coalesce(sum(input_tokens),0) AS tin, "
        "coalesce(sum(output_tokens),0) AS tout, coalesce(sum(est_cost_micros),0) AS cost, "
        "count(*) FILTER (WHERE status <> 'ok') AS bad "
        "FROM ai_usage_ledger WHERE request_id = :r GROUP BY operation",
        r=uuid.UUID(request_id),
    )
    by_op = {r["operation"]: r for r in got}
    return {
        "answer_calls": int(by_op.get("answer", {}).get("calls", 0)),
        "calls": sum(int(r["calls"]) for r in got),
        "input_tokens": sum(int(r["tin"]) for r in got),
        "output_tokens": sum(int(r["tout"]) for r in got),
        "cost_micros": sum(int(r["cost"]) for r in got),
        "failed_calls": sum(int(r["bad"]) for r in got),
    }


async def manifest_of(world: World, request_id: str) -> dict[str, Any]:
    """The request's own manifest: what was sent to the model, written BEFORE the answer was generated."""
    import uuid

    got = await rows(
        world.sm,
        "SELECT data FROM ai_manifests WHERE request_id = :r ORDER BY attempt DESC LIMIT 1",
        r=uuid.UUID(request_id),
    )
    return got[0]["data"] if got else {}


async def evidence_of(world: World, request_id: str) -> list[dict[str, Any]]:
    data = await manifest_of(world, request_id)
    passages = data.get("passages") or []
    return sorted(passages, key=lambda p: -(p.get("score") or 0.0))


def candidates_of(item: Gold, data: dict[str, Any]) -> list[dict[str, Any]]:
    """The ORIGINAL material the request selected, as recorded in its manifest before any summarization: ranked passages (best
    first) for the retrieval routes, the sections that were read for a summary, the blocks of the open document for a review.
    Each carries the locator `evidence.covers` reads, and its full original text where the manifest holds it."""
    names = {s["source_version_id"]: s["source_name"] for s in data.get("sources", [])}
    strategy = evidence.STRATEGY_OF_TASK[item.task]
    out: list[dict[str, Any]] = []
    if strategy == evidence.RANKED:
        for rank, p in enumerate(
            sorted(data.get("passages") or [], key=lambda p: -(p.get("score") or 0.0)), start=1
        ):
            loc = p.get("locator") or {}
            out.append(
                {
                    "rank": rank,
                    "label": p.get("label"),
                    "source_name": names.get(p.get("source_version_id"), ""),
                    "page_start": loc.get("page_start"),
                    "page_end": loc.get("page_end"),
                    "heading_path": loc.get("heading_path"),
                    "figure_number": loc.get("figure_number"),
                    "kind": p.get("kind"),
                    "extraction": p.get("extraction"),
                    "score": p.get("score"),
                    "text": p.get("text"),
                }
            )
    elif strategy == evidence.FULL_SECTIONS:
        for rank, sec in enumerate(data.get("sections") or [], start=1):
            out.append(
                {
                    "rank": rank,
                    "label": sec.get("label"),
                    "source_name": sec.get("source_name")
                    or names.get(sec.get("source_version_id"), ""),
                    "page_start": sec.get("page_start"),
                    "page_end": sec.get("page_end"),
                    "heading_path": sec.get("heading_path"),
                    "text": None,  # the section's original chunks are reached through the citation origins
                    "fallback": sec.get("fallback"),
                }
            )
    else:
        for i, b in enumerate(((data.get("document") or {}).get("blocks")) or []):
            out.append(
                {"rank": i + 1, "block_index": i, "document": "launch-plan", "text": b.get("text")}
            )
    return out


async def citation_origins(
    world: World, request_id: str, citations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Open every citation the way a reader does (GET .../citations/{n}/origin) and keep what came back."""
    out: dict[str, Any] = {}
    for c in citations:
        r = await world.request(world.owner, request_id, f"/citations/{c['n']}/origin")
        out[str(c["n"])] = r.json() if r.status_code == 200 else {"status": r.status_code}
    return out


def origin_resolves(origins: dict[str, Any], citations: list[dict[str, Any]]) -> bool | None:
    """STRUCTURE ONLY: does every DERIVED citation open to at least one original text whose stored hash still matches? None when
    nothing cited is derived. It does not say the originals support the summary."""
    derived = [c for c in citations if c.get("derived")]
    if not derived:
        return None
    for c in derived:
        got = origins.get(str(c["n"])) or {}
        items = got.get("origin") or []
        if not items or any(i.get("integrity") != "verified" or not i.get("text") for i in items):
            return False
    return True


async def run_item(world: EvalWorld, item: Gold, uploaded: dict[str, dict]) -> dict[str, Any]:
    collab = None
    overrides: dict[str, Any] = {"task_hint": "auto"}
    if item.document:
        collab = FakeCollab(
            world.document["id"],
            world.cfg.internal_api_secret.get_secret_value(),
            items=list(corpus.DOCUMENT_ITEMS),
        )
        overrides |= {"include_document_content": True, "document_scope": "whole"}
    if world.stub is not None:
        world.stub.classify_as = EXPECTED_TASK[item.task]
    started = time.perf_counter()
    r = await world.send(
        world.owner, item.instruction, sources=[uploaded[n] for n in item.sources], **overrides
    )
    record: dict[str, Any] = {"id": item.id, "task": item.task, "http_status": r.status_code}
    if r.status_code != 201:
        record |= {
            "outcome": f"rejected:{r.json().get('error', {}).get('code')}",
            "published": False,
        }
        return record
    rid = r.json()["id"]
    record["request_id"] = rid
    outcomes = await world.drain(collab=collab.transport() if collab else None)
    record["latency_s"] = time.perf_counter() - started
    record["outcomes"] = outcomes
    result = await world.request(world.owner, rid, "/result")
    if result.status_code != 200:
        state = (await world.request(world.owner, rid)).json()
        record |= {
            "published": False,
            "outcome": f"{state.get('state')}:{state.get('error_code')}",
            "ledger": await ledger_of(world, rid),
        }
        return record
    published = result.json()
    ledger = await ledger_of(world, rid)
    data = await manifest_of(world, rid)
    passages = await evidence_of(world, rid)
    texts = [p["text"] for p in passages]
    candidates = candidates_of(item, data)
    strategy = evidence.STRATEGY_OF_TASK[item.task]
    origins = await citation_origins(world, rid, published["citations"])
    citations = published["citations"]
    stats = published["answer"].get("stats", {})
    blocks = published["answer"]["blocks"]
    diagrams = [b for b in blocks if b.get("type") == "diagram"]
    cited_names = {c.get("source_name") for c in citations}
    coverage = published.get("coverage", {})
    per_source = coverage.get("sources") or []
    # recorded for reading (the corpus is synthetic): the published answer and its citations. Not used by any score.
    record["result"] = published
    record |= {
        "published": True,
        "outcome": "published",
        "task_type": published["task_type"],
        # the FINAL route matched the expected one. Fallback included: this is NOT a model classification
        # (see `routing`, which records what the classifier itself did)
        "task_ok": published["task_type"] == EXPECTED_TASK[item.task],
        "expected_route": EXPECTED_TASK[item.task],
        "routing": (published.get("coverage") or {}).get("routing"),
        # REQUIRED-PHRASE COVERAGE (metric phrase-v1): the share of the required phrases found in the top-k evidence passages.
        # It is a diagnostic, not recall of relevant evidence; `recall` is its deprecated alias, kept so older readers still work.
        "required_phrase_coverage": {f"@{k}": recall_at_k(texts, item.needles, k) for k in KS}
        | {"@evidence": recall_at_k(texts, item.needles, len(texts) or 1)},
        "recall": {f"@{k}": recall_at_k(texts, item.needles, k) for k in KS}
        | {"@evidence": recall_at_k(texts, item.needles, len(texts) or 1)},
        # EVIDENCE RECALL (metric evidence-v1) for ranked routes, evidence COVERAGE for routes that read whole sections or the
        # whole document: gold evidence units against the original candidates recorded in the manifest before generation
        "evidence": (
            evidence.evidence_recall(item.id, candidates)
            if strategy == evidence.RANKED
            else evidence.evidence_coverage(item.id, candidates, strategy)
        ),
        "candidates": candidates,
        "citation_origins": origins,
        "derived_citations_resolve": origin_resolves(origins, citations),
        "provenance_incomplete": stats.get("provenance_incomplete"),
        "context_notices": coverage.get("notices") or [],
        "citation_clean": stats.get("invalid_citations", 0) == 0
        and stats.get("dropped_quotes", 0) == 0,
        "answer_calls": ledger["answer_calls"],
        "no_repair": ledger["answer_calls"] == 1,
        "cites_expected_sources": (set(item.cites) <= cited_names) if item.cites else None,
        "cited_text_has_needles": (
            all(any(contains(c.get("excerpt") or "", n) for c in citations) for n in item.needles)
            if item.needles
            else None
        ),
        "figure_cited": any(bool(c.get("figure_chunk_id")) for c in citations)
        if item.expect_figure
        else None,
        "document_cited": any(str(c.get("label", "")).startswith("D") for c in citations)
        if item.document
        else None,
        "diagram_published": bool(diagrams) if item.expect_diagram else None,
        "diagram_renders": (
            bool(diagrams) and all(b.get("render_status") == "ok" for b in diagrams)
            if item.expect_diagram
            else None
        ),
        "coverage_full": (
            bool(per_source)
            and all(
                s.get("sections_read") == s.get("sections_total")
                for s in per_source
                if "sections_total" in s
            )
            if item.task in ("summarize", "compare")
            else None
        ),
        "ledger": ledger,
    }
    return record


def fmt(value: float | None, pct: bool = False, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.0f}%" if pct else f"{value:.{digits}f}"


def failure_lines(run: dict | None) -> list[str]:
    if not run:
        return ["n/a (no run-wide ledger was available)"]
    f = run["failures"]
    head = f"Failed provider calls: {ratio(f['calls'])} of all recorded calls."
    rows_ = [
        f"| {r['operation']} | {r['error_code']} | {r['calls']} |"
        for r in f["by_operation_and_code"]
    ]
    if not rows_:
        return [head, "", "None."]
    return [head, "", "| Operation | Error code | Calls |", "|---|---|---|", *rows_]


def cost_lines(run: dict | None) -> list[str]:
    if not run:
        return ["n/a"]
    c = run["cost"]
    shown = (
        "unknown"
        if c["est_cost_micros"] is None
        else f"{c['est_cost_usd']:.6f} USD ({c['kind_of_value']})"
    )
    u = c["usage_retained"]
    return [
        f"* Status: **{c['status']}**. Cost: **{shown}**.",
        f"* Tokens (kept even when pricing is missing): generation {u['generation_input_tokens']} in / "
        f"{u['generation_output_tokens']} out; embeddings {u['embedding_input_tokens']} in.",
        "* A ledger or table cost of 0 means not priced, never free. "
        + c["provider_reported_cost"]
        + ".",
        f"* Rates: {c['rates'] or 'none configured'}.",
    ]


def cost_cell(run: dict | None, led: list[dict]) -> str:
    """Cost per task for the table. Unknown stays unknown: a 0 in the ledger is not a price."""
    status = ((run or {}).get("cost") or {}).get("status")
    if status is None:
        return "n/a"
    if status == "unpriced":
        return "unknown (no rates configured)"
    value = fmt(mean([x["cost_micros"] / 1_000_000 for x in led]), digits=5)
    return value + (" (partial)" if status == "partial" else "")


def ratio(f: dict) -> str:
    """ "a/b (p%)" for a fraction() dict: the numerator and denominator are always shown."""
    pct = "n/a" if f["value"] is None else f"{f['value'] * 100:.0f}%"
    return f"{f['numerator']}/{f['denominator']} ({pct})"


def evidence_lines(ev: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
    """Evidence recall (ranked routes) and evidence coverage (whole-section and whole-document routes), kept apart."""
    v = evidence.EVIDENCE_METRIC_VERSION
    lines = []
    for k in ("@1", "@3", "@5", "@all"):
        r = ev["recall"][k]
        lines.append(
            f"| evidence_recall{k} ({v}: share of gold evidence units covered by the top-ranked ORIGINAL passages; "
            f"{r['items']} ranked items with gold, {r['found_units']}/{r['needed_units']} units pooled) | "
            f"{fmt(r['mean'], True)} | measured against evidence-gold ({evidence.GOLD_VERSION}) |"
        )
    c = ev["coverage"]
    lines.append(
        f"| evidence_coverage ({v}: gold units covered by the sections or document READ, unranked; {c['items']} items, "
        f"{c['found_units']}/{c['needed_units']} units pooled; NOT comparable with recall) | {fmt(c['mean'], True)} | measured |"
    )
    lines.append(
        f"| Derived citations that open to verified original text | "
        f"{ratio_of([r.get('derived_citations_resolve') for r in records])} | structure only: lineage, not support |"
    )
    return lines


def ratio_of(flags: list[bool | None]) -> str:
    applicable = [f for f in flags if f is not None]
    return f"{sum(1 for f in applicable if f)}/{len(applicable)}" if applicable else "n/a"


def build_report(
    mode: str, ocr: float | None, analysis: dict, records: list[dict], run: dict | None = None
) -> str:
    stub = mode == "stub"
    pub = [r for r in records if r.get("published")]
    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_task[r["task"]].append(r)
    latencies = [r["latency_s"] for r in records if "latency_s" in r]

    def pcov(
        r: dict, k: str
    ):  # the phrase coverage of a record; results written before 2026-11-20 call it `recall`
        return (r.get("required_phrase_coverage") or r.get("recall") or {}).get(k)

    recall = {
        k: share([pcov(r, k) == 1.0 for r in pub if pcov(r, k) is not None])
        for k in ("@1", "@3", "@5", "@evidence")
    }
    # phrase coverage as the mean fraction of needles found (finer than "all found")
    recall_mean = {
        k: mean([pcov(r, k) for r in pub if pcov(r, k) is not None])
        for k in ("@1", "@3", "@5", "@evidence")
    }
    ev = evidence.summarize_evidence(pub)
    model_note = "stub: NOT a model measurement" if stub else "measured"
    rm = routing_metrics(records)
    lines = [
        f"# Evaluation results ({'local provider stubs' if stub else 'REAL providers'})",
        "",
        f"Run: {dt.datetime.now(dt.UTC).strftime('%Y-%m-%d %H:%M UTC')}. Gold items: {len(records)}; "
        f"published: {len(pub)}. Sources: {', '.join(corpus.sources())}.",
        "",
        "| Metric | Value | Basis |",
        "|---|---|---|",
        f"| Requests that published an answer | {fmt(share([r.get('published') for r in records]), True)} "
        f"({len(pub)}/{len(records)}) | measured (real request pipeline) |",
        f"| required_phrase_coverage@1 ({METRIC_VERSIONS['required_phrase_coverage']}: mean share of required phrases found in the top-1 evidence passages; NOT retrieval recall) | {fmt(recall_mean['@1'], True)} | "
        f"{'measured: real chunking, SQL full-text and ranking; vector part is a topic-bag stand-in' if stub else 'measured'} |",
        f"| required_phrase_coverage@3 | {fmt(recall_mean['@3'], True)} | same |",
        f"| required_phrase_coverage@5 | {fmt(recall_mean['@5'], True)} | same |",
        f"| required_phrase_coverage over all evidence sent to the model | {fmt(recall_mean['@evidence'], True)} | same |",
        f"| Items with every required phrase in the evidence | {fmt(recall['@evidence'], True)} | same |",
        *evidence_lines(ev, pub),
        f"| OCR character accuracy (3 scanned pages) | {fmt(ocr, True)} | measured (real Tesseract, "
        f"{'identical in both modes' if stub else 'with the vision fallback available'}) |",
        f"| Final route matched the expected route (fallback included) | {ratio(rm['final_route_accuracy'])} | "
        f"{'stub: the classifier answer is scripted' if stub else 'measured'}; NOT a model classification, see Routing |",
        f"| Citations without an invalid reference or dropped quote | {fmt(share([r.get('citation_clean') for r in pub]), True)} | {model_note} |",
        f"| Answers that cite the expected source(s) | {fmt(share([r.get('cites_expected_sources') for r in pub]), True)} | {model_note} |",
        f"| Cited text contains the required phrase | {fmt(share([r.get('cited_text_has_needles') for r in pub]), True)} | {model_note} |",
        f"| Schema-valid answers without a repair call | {fmt(share([r.get('no_repair') for r in pub]), True)} | {model_note} |",
        f"| Diagram requests that published a diagram | {fmt(share([r.get('diagram_published') for r in pub]), True)} | {model_note} |",
        f"| Diagrams that render (Mermaid parses) | {fmt(share([r.get('diagram_renders') for r in pub]), True)} | {model_note} |",
        f"| Figure questions that cite a figure | {fmt(share([r.get('figure_cited') for r in pub]), True)} | {model_note} |",
        f"| Summaries/comparisons with full section coverage | {fmt(share([r.get('coverage_full') for r in pub]), True)} | measured (server-computed coverage) |",
        f"| Latency p50 / p95 per request (s) | {fmt(percentile(latencies, 50))} / {fmt(percentile(latencies, 95))} | "
        f"{'stub: pipeline time only, no network or model time' if stub else 'measured, end to end'} |",
        "",
        "## Per task type",
        "",
        "| Task | Items | Published | Calls | Input tokens | Output tokens | Est. cost (USD) | "
        "Latency p50 (s) | Basis for tokens/cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for task, items in sorted(by_task.items()):
        led = [r["ledger"] for r in items if "ledger" in r]
        lat = [r["latency_s"] for r in items if "latency_s" in r]
        lines.append(
            f"| {task} | {len(items)} | {sum(1 for r in items if r.get('published'))} | "
            f"{fmt(mean([x['calls'] for x in led]), digits=1)} | "
            f"{fmt(mean([x['input_tokens'] for x in led]), digits=0)} | "
            f"{fmt(mean([x['output_tokens'] for x in led]), digits=0)} | "
            f"{cost_cell(run, led)} | "
            f"{fmt(percentile(lat, 50))} | {'stub placeholder usage' if stub else 'provider-reported'} |"
        )
    lines += [
        "",
        "(per-item means across the request's own calls; analysis of the sources is listed below)",
        "",
        "## Routing (the classifier and the fallback)",
        "",
        "| Measure | Value (numerator/denominator) |",
        "|---|---|",
        f"| Requests that published | {ratio(rm['published'])} |",
        f"| Valid model classifications / classifier attempts | {ratio(rm['valid_model_classifications'])} |",
        f"| Model classification accuracy, of the valid ones | {ratio(rm['model_accuracy_of_valid'])} |",
        f"| Model classification accuracy, of all attempts (a failure counts as not correct) | {ratio(rm['model_accuracy_of_attempts'])} |",
        f"| Classifier failures | {ratio(rm['classifier_failures'])}; by category: {rm['classifier_failures_by_category'] or 'none'} |",
        f"| Requests that ran the fallback | {ratio(rm['fallback_requests'])} ({', '.join(rm['fallback_ids']) or 'none'}) |",
        f"| Final route correct, fallback included | {ratio(rm['final_route_accuracy'])}, of which reached only by the fallback: {rm['final_correct_by_fallback']} |",
        f"| Valid model classifications that differ from the expected route | {rm['model_misrouted'] or 'none'} |",
        "",
        "A request that is right because of the fallback counts as a classifier failure, a fallback execution and a correct "
        "final route. It never counts as a model classification.",
        "",
        "## Provider failures (run-wide usage ledger)",
        "",
        *failure_lines(run),
        "",
        "## Cost",
        "",
        *cost_lines(run),
        "",
        "## Analysis of the sources",
        "",
        "| Source | State | Chunks | Figures | Warnings |",
        "|---|---|---|---|---|",
    ]
    for name, st in analysis.items():
        lines.append(
            f"| {name} | {st.get('state')} | {st.get('chunk_count')} | {st.get('figure_count')} | "
            f"{', '.join(map(str, st.get('warnings') or [])) or '-'} |"
        )
    failures = [r for r in records if not r.get("published")]
    lines += ["", "## Items that did not publish", ""]
    lines += [f"* {r['id']}: {r.get('outcome')}" for r in failures] or ["None."]
    lines += [
        "",
        "## How to read this",
        "",
        "* Gold set: " + "; ".join(f"{g.id} ({g.task})" for g in GOLD) + ".",
        '* "Basis" says what each number actually measures. In stub mode a model-dependent number only'
        " shows that the validation and publication code handles a well-behaved answer; it says nothing"
        " about how any real model will do. Run `EVAL_PROVIDER=real` for that.",
        "* Small corpus (4 sources, 13 questions, one question per fact): treat differences of a few points as noise.",
        "",
        "## What each measure is, and is not",
        "",
        "* **Recall@k** counts RETRIEVED PASSAGES (the evidence items sent to the model, chunk-level, ranked by retrieval score; "
        "not pages). Expected evidence = the gold item's required phrases (`needles`); a phrase is found when it occurs, "
        "normalised for case and spacing, in one of the top-k passages. Items with no phrases are left out of recall.",
        "* **Citation checks are mechanical.** 'No invalid reference or dropped quote' is the system's own validation (every cited "
        "label exists; every quote occurs in its passage). It does NOT require an answer to cite anything. 'Cites the expected "
        "source' and 'cited text contains the phrase' are string checks.",
        "* **Denominators.** Quality metrics are over PUBLISHED requests; a request that did not publish is listed under "
        "'Items that did not publish' and counts against 'Requests that published'. Provider failures that a request survived "
        "are shown under Provider failures, not folded into any rate.",
        "* **Not assessed here:** whether an answer is factually supported by its source, or useful. Nothing in this file is a "
        "human judgement of either. The evaluator has no pass/fail threshold; pytest passing means the harness ran.",
    ]
    return "\n".join(lines) + "\n"


@pytest.mark.skipif(MODE not in ("stub", "real"), reason="EVAL_PROVIDER must be stub or real")
async def test_evaluate(make_actor, sessionmaker, tmp_path, redis_prefix):
    world = await build(make_actor, sessionmaker, tmp_path, redis_prefix)
    # stage 1 of the manifest: everything knowable before the first provider call, captured now
    run_manifest = manifest.begin(
        kind="answer",
        mode=MODE,
        settings=world.cfg,
        expected_items=len(GOLD),
        dataset={
            "questions": len(GOLD),
            "gold_sha256": manifest.sha256_json(list(GOLD)),
            "corpus_py_sha256": manifest.sha256_file(Path(corpus.__file__)),
            "sources_sha256": {n: manifest.sha256_bytes(b) for n, b in corpus.sources().items()},
            # added 2026-11-20: the evidence gold, the identity of the fixtures it names and the metric versions. A run
            # that reports `evidence-v1` numbers is only comparable with another that has the same fingerprint.
            "evidence_gold": {
                "version": evidence.GOLD_VERSION,
                "fingerprint": evidence.gold_fingerprint(),
                "fixtures": evidence.FIXTURE_VERSION,
                "fixture_identity": evidence.corpus_identity(),
                "strategies": evidence.STRATEGY_OF_TASK,
                "policies": evidence.policies(),
            },
            "metric_versions": METRIC_VERSIONS,
        },
    )
    started = dt.datetime.fromisoformat(run_manifest["started_utc"])
    current: dict[str, Gold | None] = {"item": None}
    if world.stub is not None:
        world.stub.answer_fn = simulated_model(current)

    uploaded: dict[str, dict] = {}
    for name, data in corpus.sources().items():
        uploaded[name] = await world.upload(world.owner, name, data)
        assert (await world.analyze(world.owner, uploaded[name])).status_code == 202
    outcomes = await world.run_worker(limit=60)
    analysis = {name: await world.status(world.owner, src) for name, src in uploaded.items()}
    analysis_summary = {
        n: {
            "state": s.get("state") or s.get("analysis_state"),
            "chunk_count": s.get("chunk_count"),
            "figure_count": s.get("figure_count"),
            "warnings": s.get("warnings"),
        }
        for n, s in analysis.items()
    }

    scan = (await world.passages(world.owner, uploaded[corpus.SCAN_NAME])).json()["passages"]
    ocr = character_accuracy(corpus.SCAN_TRUTH, " ".join(p["text"] for p in scan))

    records = []
    for item in GOLD:
        current["item"] = item
        records.append(await run_item(world, item, uploaded))

    ledger = await manifest.ledger_summary(world.sm, started)
    run = {"failures": failed_calls(ledger), "cost": manifest.cost_summary(world.cfg, ledger)}
    report = build_report(MODE, ocr, analysis_summary, records, run)
    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"eval-{MODE}-{stamp}.json").write_text(
        json.dumps(
            {
                "mode": MODE,
                "ocr": ocr,
                "analysis": analysis_summary,
                "run": run,
                "records": records,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    (RESULTS / f"{MODE}-results.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    # stage 2: what exists only now (usage, failures, cost, metrics)
    run_manifest = await manifest.finish(
        run_manifest,
        settings=world.cfg,
        sessionmaker=world.sm,
        records=records,
        results={"routing": routing_metrics(records), "ocr_character_accuracy": ocr},
    )
    manifest.write(RESULTS / f"eval-{MODE}-{stamp}.manifest.json", run_manifest)

    # the harness itself must have worked: sources analysed and the pipeline answered the gold set
    assert outcomes and set(outcomes) == {"succeeded"}, outcomes
    assert any(r.get("published") for r in records), "no gold item published an answer"
