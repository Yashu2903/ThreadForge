"""The fresh comparison of the review follow-up: baseline against the new build, one run each, metric versions named.

    cd backend
    PYTHONPATH=. uv run python -m tests.eval.build_followup_comparison

Reads only committed records in docs/evidence/ai-eval/ and writes `followup-comparison.md` and `.json` next to them. It calls no provider.

WHAT IS COMPARED, AND WHAT IS NOT
  * The BASELINE is the build that was reviewed (`0fe9a95`, the app code of the first review sheet). The quality dataset and the evidence
    metrics were run on it with the NEW harness (the worktree run), so every number below is computed by the same code on both builds. The
    earlier runs (`none-answer`, `postfix-proposal`) are shown where the dataset is unchanged, as the historical record.
  * One sample per item per run. A single item flipping is noise; the report says so beside every table.
  * A metric that a baseline record cannot support (the old manifests keep no section locators for a summary, the old build has no citation
    origin route) is shown as `n/a`, never as 0.
  * Metric versions: required_phrase_coverage = phrase-v1 (historically called "recall"), evidence_recall = evidence-v1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.eval import evidence, quality
from tests.eval.metrics import METRIC_VERSIONS, mean, percentile, routing_metrics, share

EV = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "ai-eval"


def load(name: str) -> dict[str, Any] | None:
    p = EV / f"{name}.records.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def manifest(name: str) -> dict[str, Any]:
    p = EV / f"{name}.manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def frac(num: int, den: int) -> str:
    return f"{num}/{den}" if den else "n/a"


# ---- quality ---------------------------------------------------------------------------------------------------------------


def quality_section() -> tuple[list[str], dict[str, Any]]:
    base = load("baseline-quality")
    new_dev, new_held = load("new-quality-dev"), load("new-quality-heldout")
    if not (base and new_dev and new_held):
        return ["(quality runs are not in the evidence folder yet)"], {}
    by = {
        "baseline": {r["id"]: r for r in base["records"]},
        "new": {r["id"]: r for r in new_dev["records"] + new_held["records"]},
    }
    out: dict[str, Any] = {"findings": {}, "items": {}}
    lines = [
        "## 1. Task quality (dataset `quality-v1`; criteria declared before the first run, `tests/eval/quality.py`)",
        "",
        "Finding met = the item satisfied its finding's criterion by the deterministic checks (not a human judgement). One sample per item per run.",
        "",
        "| Finding | Split | Items | Baseline (0fe9a95) met | New build met |",
        "|---|---|---|---|---|",
    ]
    for f in ("F1", "F2", "F3", "F4", "F5", "F6", "F7"):
        for sp in ("dev", "heldout"):
            items = [i for i in quality.ITEMS if i.finding == f and i.split == sp]
            if not items:
                continue
            b = sum(1 for i in items if by["baseline"][i.id]["score"]["finding_met"])
            n = sum(1 for i in items if by["new"][i.id]["score"]["finding_met"])
            out["findings"][f"{f}/{sp}"] = {"items": len(items), "baseline": b, "new": n}
            lines.append(f"| {f} | {sp} | {len(items)} | {b} | {n} |")
    lines += [
        "",
        "| Item | Split | Finding | Baseline | New | Baseline: why not met |",
        "|---|---|---|---|---|---|",
    ]
    for i in quality.ITEMS:
        b, n = by["baseline"][i.id], by["new"][i.id]
        out["items"][i.id] = {
            "baseline": b["score"]["finding_met"],
            "new": n["score"]["finding_met"],
        }
        why = "" if b["score"]["finding_met"] else "; ".join(b["score"]["explain"])[:230]
        lines.append(
            f"| {i.id} | {i.split} | {i.finding} | {'met' if b['score']['finding_met'] else 'NOT met'} | {'met' if n['score']['finding_met'] else 'NOT met'} | {why} |"
        )
    # F5: the app's flag
    fb = sum(
        1
        for r in by["baseline"].values()
        for v in (r["score"]["semantic"].get("lost_and_flagged") or {}).values()
    )
    fn = sum(
        1
        for r in by["new"].values()
        for v in (r["score"]["semantic"].get("lost_and_flagged") or {}).values()
    )
    lines += [
        "",
        f"Rewritten sentences that lost a fact of their rubric: baseline {fb}, new build {fn}. (The baseline has no review flag, so none of its losses was marked.)",
        "",
        "Reading these numbers: the two dev diagram items and the closing-summary and missing-context items reproduce the reviewed defects on the baseline and not on the new "
        "build. The baseline's two held-out diagrams both said the order was assumed (one is marked not met only because the invented-label heuristic flagged "
        "'Sam approves equipment requests', a harmless paraphrase of 'approved by Sam': a false positive of the check, kept in the numbers). The review-wording finding F7 "
        "(the first review's A5) did NOT reproduce on the baseline in these two samples, so this run gives no evidence that the review prompt change did anything; it was made on the "
        "strength of the single reviewed output. F3 and F2 are met by construction (the baseline has no origin route and no notice).",
    ]
    return lines, out


# ---- the 13-question answer evaluation ----------------------------------------------------------------------------------------


def answer_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    pub = [r for r in records if r.get("published")]

    def pcov(r, k):
        return (r.get("required_phrase_coverage") or r.get("recall") or {}).get(k)

    rm = routing_metrics(records)

    def usable(r: dict[str, Any]) -> bool:
        """An evidence result is usable unless it is a summary coverage computed from a manifest that kept no section locators (the
        reviewed build's): that would read as 0 coverage for a reason that has nothing to do with what was read."""
        e = r.get("evidence")
        if not e:
            return False
        if e.get("strategy") == "full_sections":
            return any(
                c.get("heading_path") or c.get("page_start") for c in r.get("candidates", [])
            )
        return True

    ev = (
        evidence.summarize_evidence([r for r in pub if usable(r)])
        if any(usable(r) for r in pub)
        else None
    )
    lat = [r["latency_s"] for r in records if "latency_s" in r]
    led = [r["ledger"] for r in records if "ledger" in r]
    return {
        "published": f"{len(pub)}/{len(records)}",
        "final_route": f"{rm['final_route_accuracy']['numerator']}/{rm['final_route_accuracy']['denominator']}",
        "model_classification": f"{rm['model_accuracy_of_valid']['numerator']}/{rm['model_accuracy_of_valid']['denominator']}",
        "classifier_failures": f"{rm['classifier_failures']['numerator']}/{rm['classifier_failures']['denominator']}",
        "phrase": {
            k: mean([pcov(r, k) for r in pub if pcov(r, k) is not None])
            for k in ("@1", "@3", "@5", "@evidence")
        },
        "phrase_items": sum(1 for r in pub if pcov(r, "@3") is not None),
        "evidence": ev,
        "citations_clean": share([r.get("citation_clean") for r in pub]),
        "cites_expected": share([r.get("cites_expected_sources") for r in pub]),
        "cited_text_has_phrase": share([r.get("cited_text_has_needles") for r in pub]),
        "derived_resolve": [
            r.get("derived_citations_resolve") for r in pub if "derived_citations_resolve" in r
        ],
        "diagram_renders": share([r.get("diagram_renders") for r in pub]),
        "diagram_disclosed": [
            r.get("diagram_order_disclosed") for r in pub if r.get("diagram_draws_order")
        ],
        "latency_p50": percentile(lat, 50),
        "latency_p95": percentile(lat, 95),
        "tokens_in": sum(x["input_tokens"] for x in led),
        "tokens_out": sum(x["output_tokens"] for x in led),
        "failed_calls": sum(x["failed_calls"] for x in led),
        "calls": sum(x["calls"] for x in led),
    }


def answer_section() -> tuple[list[str], dict[str, Any]]:
    new = load("new-answer")
    hist = load("none-answer")
    base = load("baseline-answer")  # the reviewed build, run with the new harness
    if not new:
        return ["(the new answer run is not in the evidence folder yet)"], {}
    cols = {
        "none-answer (historical, old harness)": hist,
        "baseline-answer (0fe9a95, new harness)": base,
        "new-answer (new build)": new,
    }
    m = {k: answer_metrics(v["records"]) for k, v in cols.items() if v}
    names = list(m)
    out = {"columns": names, "metrics": m}
    lines = [
        "## 2. The 13-question answer evaluation (the acceptance dataset, unchanged)",
        "",
        "Same 13 questions and gold expectations in every column; classifier effort `none` in all three. `none-answer` was produced by the earlier harness "
        "(`eval-2`): it has required-phrase coverage (named `recall` then) but no ranked candidates, so no evidence-v1 numbers. The other two ran the current harness (`eval-3`).",
        "",
        "| Measure (metric version) | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]

    def row(label: str, f) -> None:
        lines.append(f"| {label} | " + " | ".join(f(m[n]) for n in names) + " |")

    row("Published", lambda x: x["published"])
    row("Final route correct (fallback included)", lambda x: x["final_route"])
    row("Model classification correct, of valid", lambda x: x["model_classification"])
    row("Classifier failures", lambda x: x["classifier_failures"])
    for k in ("@1", "@3", "@5"):
        row(
            f"required_phrase_coverage{k} (phrase-v1; mean share of required phrases in the top-k passages; NOT retrieval recall)",
            lambda x, k=k: pct(x["phrase"][k]),
        )
    for k in ("@1", "@3", "@5", "@all"):
        row(
            f"evidence_recall{k} (evidence-v1; gold evidence units in the top-k ORIGINAL passages; pooled units)",
            lambda x, k=k: (
                "n/a (no ranked candidates recorded)"
                if not x["evidence"]
                else f"{pct(x['evidence']['recall'][k]['mean'])} ({x['evidence']['recall'][k]['found_units']}/{x['evidence']['recall'][k]['needed_units']} units, {x['evidence']['recall'][k]['items']} items)"
            ),
        )
    row(
        "evidence_coverage (evidence-v1; unranked routes: summarize, review)",
        lambda x: (
            "n/a"
            if not x["evidence"]
            else f"{pct(x['evidence']['coverage']['mean'])} ({x['evidence']['coverage']['found_units']}/{x['evidence']['coverage']['needed_units']} units, {x['evidence']['coverage']['items']} items)"
        ),
    )
    row(
        "Citations without an invalid reference or dropped quote",
        lambda x: pct(x["citations_clean"]),
    )
    row("Cites the expected source(s)", lambda x: pct(x["cites_expected"]))
    row("Cited text contains the required phrase", lambda x: pct(x["cited_text_has_phrase"]))
    row(
        "Derived citations that open to verified original text",
        lambda x: (
            "n/a"
            if not x["derived_resolve"]
            else frac(
                sum(1 for v in x["derived_resolve"] if v),
                len([v for v in x["derived_resolve"] if v is not None]),
            )
        ),
    )
    row("Diagrams that render", lambda x: pct(x["diagram_renders"]))
    row(
        "Diagrams that draw directed relations and say they are assumed",
        lambda x: (
            "n/a"
            if not x["diagram_disclosed"]
            else frac(sum(1 for v in x["diagram_disclosed"] if v), len(x["diagram_disclosed"]))
        ),
    )
    row("Failed provider calls / calls", lambda x: f"{x['failed_calls']}/{x['calls']}")
    row(
        "Latency p50 / p95 (s)",
        lambda x: (
            f"{x['latency_p50']:.1f} / {x['latency_p95']:.1f}"
            if x["latency_p50"] is not None
            else "n/a"
        ),
    )
    row("Tokens in / out (all calls)", lambda x: f"{x['tokens_in']} / {x['tokens_out']}")
    lines += [
        "",
        "evidence_coverage of the baseline column leaves out the summary item: the reviewed build's manifest keeps no section locators, so its coverage cannot be computed (not 0).",
        "",
        "Cost: unknown in every run (no prices configured); a 0 in a ledger means not priced, never free.",
        "",
        "Why the two phrase and evidence rows differ: required_phrase_coverage asks whether a PHRASE is in the top-k passages; evidence_recall asks whether the right PLACE (page, section, figure) is. "
        "They agree when the phrase sits in the right place and disagree when a paraphrase answers (phrase 0, evidence 1) or when the phrase sits in the wrong place (phrase 1, evidence 0).",
    ]
    return lines, out


# ---- the proposal gate -------------------------------------------------------------------------------------------------------------


def proposal_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    pub = [r for r in records if r.get("published")]
    lat = [r["latency_s"] for r in records if "latency_s" in r]
    return {
        "items": len(records),
        "first_try": f"{sum(1 for r in records if r.get('first_try_valid'))}/{len(records)}",
        "published": f"{len(pub)}/{len(records)}",
        "gate": len(pub) / len(records) if records else None,
        "resolves": share([r["resolves"] for r in pub]),
        "citation_validity": mean(
            [r["citation_valid"] for r in pub if r.get("citation_valid") is not None]
        ),
        "repaired": sum(1 for r in records if r.get("repaired")),
        "review_flags": sum(len(f["flags"]) for r in pub for f in r.get("review_flags", [])),
        "p50": percentile(lat, 50),
        "p95": percentile(lat, 95),
        "calls": mean([r["ledger"]["answer_calls"] for r in records if "ledger" in r]),
    }


def proposal_section() -> tuple[list[str], dict[str, Any]]:
    new, hist = load("new-proposal"), load("postfix-proposal")
    if not new:
        return ["(the new proposal run is not in the evidence folder yet)"], {}
    cols = {"postfix-proposal (reviewed build)": hist, "new-proposal (new build)": new}
    m = {k: proposal_metrics(v["records"]) for k, v in cols.items() if v}
    names = list(m)
    lines = [
        "## 3. The proposal gate (schema validity, unchanged rule: >= 90% valid with at most one repair)",
        "",
        "| Measure | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
        "| Gold items | " + " | ".join(str(m[n]["items"]) for n in names) + " |",
        "| Schema-valid WITHOUT repair | " + " | ".join(m[n]["first_try"] for n in names) + " |",
        "| Schema-valid WITH one repair (published) | "
        + " | ".join(m[n]["published"] + f" ({pct(m[n]['gate'])})" for n in names)
        + " |",
        "| Base hashes, anchors and replay resolve | "
        + " | ".join(pct(m[n]["resolves"]) for n in names)
        + " |",
        "| Citation validity (mean per proposal; not a check of support) | "
        + " | ".join(pct(m[n]["citation_validity"]) for n in names)
        + " |",
        "| Items that used the repair | " + " | ".join(str(m[n]["repaired"]) for n in names) + " |",
        "| Qualification review flags raised (new check; a note, not a verdict) | "
        + " | ".join(str(m[n]["review_flags"]) if n.startswith("new") else "n/a" for n in names)
        + " |",
        "| Model calls per request (mean) | "
        + " | ".join(f"{m[n]['calls']:.2f}" for n in names)
        + " |",
        "| Latency p50 / p95 (s) | "
        + " | ".join(f"{m[n]['p50']:.1f} / {m[n]['p95']:.1f}" for n in names)
        + " |",
        "",
        "The gate measures that proposals pass the document engine. It does not measure that they are right: see section 1 for task quality and the review sheet for the outputs.",
    ]
    return lines, {"columns": names, "metrics": m}


def guards(a: dict[str, Any], p: dict[str, Any]) -> list[str]:
    lines = ["## 4. The regression guards (declared in `tests/eval/quality.py` before any run)", ""]
    if p:
        n = p["metrics"].get("new-proposal (new build)")
        lines.append(
            f"* **G1** proposal schema gate >= 90% with at most one repair: **{'met' if n and n['gate'] is not None and n['gate'] >= 0.9 else 'NOT met'}** ({n['published'] if n else 'n/a'})."
        )
    if a:
        n = a["metrics"].get("new-answer (new build)")
        if n:
            fr = n["final_route"].split("/")
            ok = int(fr[0]) >= 12 and n["classifier_failures"].startswith("0/")
            lines.append(
                f"* **G2** final route correct on >= 12 of 13 and no classifier failure: **{'met' if ok else 'NOT met'}** (final route {n['final_route']}, classifier failures {n['classifier_failures']})."
            )
        hist = load("none-answer")
        new = load("new-answer")
        if hist and new:
            was = {r["id"] for r in hist["records"] if r.get("published")}
            now = {r["id"] for r in new["records"] if r.get("published")}
            lines.append(
                f"* **G3** no item that published at baseline fails to publish: **{'met' if was <= now else 'NOT met'}** (baseline {len(was)}, new {len(now)}; missing now: {sorted(was - now) or 'none'})."
            )
    return lines


def main() -> int:
    q_lines, q = quality_section()
    a_lines, a = answer_section()
    p_lines, p = proposal_section()
    head = [
        "# The review follow-up: baseline against the new build",
        "",
        "Generated by `backend/tests/eval/build_followup_comparison.py` from the committed records; it calls no provider. "
        f"Metric versions: {', '.join(f'{k} = {v}' for k, v in METRIC_VERSIONS.items())}. Dataset `{quality.DATASET_VERSION}`, evidence gold `{evidence.GOLD_VERSION}`.",
        "",
        "Baseline = the build that was reviewed (`0fe9a95`); new build = the commit named in each run's manifest. Code, configuration, prompt versions "
        "(answer-v1/edit-v1 against answer-v2/edit-v2), schema hashes and dataset fingerprints are in the manifests; one run of each. "
        "With so few items, differences of a single item are noise, and these are measurements of one model on one synthetic corpus.",
        "",
    ]
    tail = ["", *guards(a, p), ""]
    text = "\n".join(head + q_lines + [""] + a_lines + [""] + p_lines + tail) + "\n"
    (EV / "followup-comparison.md").write_text(text, encoding="utf-8")
    (EV / "followup-comparison.json").write_text(
        json.dumps({"quality": q, "answer": a, "proposal": p}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"wrote followup-comparison.md ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
