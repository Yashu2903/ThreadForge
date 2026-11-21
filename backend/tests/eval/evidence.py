"""Gold EVIDENCE for the answer evaluation: which original material a correct answer to each item rests on, by stable
identifier, so that retrieval can be measured as retrieval (metric `evidence-v1`).

WHY THIS EXISTS. The first evaluation reported "retrieval recall@k". What it computed was the share of REQUIRED PHRASES found in
the top-k passages (`required_phrase_coverage`, metric `phrase-v1`, see metrics.py). That is a useful diagnostic, but it is not
recall of relevant evidence: a paraphrased passage that answers the question scores zero, an irrelevant passage that happens to
contain the phrase scores one, and a route that does not retrieve passages (summarize) scores zero whatever it read (A7 of the
review sheet). This module defines the other measure and leaves the first one in place, under its true name.

THE UNIT OF EVIDENCE is an identifier of a place in an ORIGINAL file, never a chunk id, an ordinal or a retrieval score
(those change when the index is rebuilt):

    <file>#page=<n>              a page of a PDF (the text layer, or the OCR of a scanned page)
    <file>#section=<A > B>       a heading path of a Markdown or Word file
    <file>#figure=<n>            the n-th figure of a Word file or PDF
    <doc>#block=<i>              the i-th (0-based) text block of an open document

A retrieved or selected candidate COVERS the units its own locator names: a chunk spanning pages 2-3 covers `page=2` and
`page=3`; a chunk under heading path `Release process > Deploy` covers that section and every section below it. Matching is by
locator, so a paraphrase, a different chunking or another phrase in the same place all count the same.

THE POLICIES (also written into every run's manifest):
  * ACCEPTABLE SETS. An item lists one or more acceptable sets of units; a set is satisfied when EVERY unit in it is covered.
    The item's value is the best set's covered share, so several correct ways to answer do not penalise either.
  * MULTIPLE RELEVANT PASSAGES. A unit covered by several candidates counts once, at the rank of the first.
  * recall@k = covered units of the best set among the top-k candidates / units in that set. Its denominator per item is the
    size of the best set; across items it is the mean over items that have gold evidence, reported with the item count, and
    pooled as covered/needed units.
  * EMPTY OR MISSING GOLD. An item with no gold evidence has recall None: it is left out and counted under `without_gold`.
    It is never scored 0 or 1.
  * STRATEGY. Recall@k is defined only for a RANKED strategy (`ranked_retrieval`, per source for compare). A route that reads
    whole sections (`full_sections`, summarize) or the whole document (`full_document`, review) has no ranking: it reports
    COVERAGE (units covered by what was read), a different measure that is never averaged with recall.
  * FIXTURE IDENTITY. Gold units name a fixture file; `fixture_identity` pins its content (bytes where the generator is
    deterministic, otherwise a hash of the content it is generated from), so a regenerated or edited fixture is detected
    before any score is read.

The gold below is derived from the fixtures' own text and is checked against it by tests/test_eval_evidence.py: it is never
edited to fit a model's output.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

from tests.eval import corpus

EVIDENCE_METRIC_VERSION = "evidence-v1"
GOLD_VERSION = "evidence-gold-v1"
FIXTURE_VERSION = "corpus-v1"

RANKED = "ranked_retrieval"
FULL_SECTIONS = "full_sections"
FULL_DOCUMENT = "full_document"
STRATEGY_OF_TASK = {
    "qa": RANKED,
    "figure": RANKED,
    "diagram": RANKED,
    "compare": RANKED,  # ranked within each source, balanced across them
    "summarize": FULL_SECTIONS,
    "review": FULL_DOCUMENT,
}


@dataclass(frozen=True)
class EvidenceGold:
    """The acceptable sets of evidence units for one gold item, at one granularity."""

    granularity: str  # page | section | figure | document_block
    sets: tuple[tuple[str, ...], ...]
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "granularity": self.granularity,
            "sets": [list(s) for s in self.sets],
            "note": self.note,
        }


def _page(name: str, *pages: int) -> tuple[str, ...]:
    return tuple(f"{name}#page={p}" for p in pages)


RELEASE = "release.md"
DEPLOY = f"{RELEASE}#section=Release process > Deploy"
BUILD = f"{RELEASE}#section=Release process > Build"
ROOT = f"{RELEASE}#section=Release process"

# item id -> gold evidence. Items with no entry, or an empty `sets`, have no gold evidence (recall is None, not 0).
GOLD_EVIDENCE: dict[str, EvidenceGold] = {
    "qa-berlin": EvidenceGold("page", (_page("depots.pdf", 1),)),
    "qa-lisbon-storms": EvidenceGold("page", (_page("depots.pdf", 2),)),
    "qa-scan-orders": EvidenceGold("page", (_page("march-scan.pdf", 1),)),
    "qa-scan-delays": EvidenceGold("page", (_page("march-scan.pdf", 1),)),
    "qa-md-owner": EvidenceGold("section", ((DEPLOY,),), "the owner table sits under Deploy"),
    "qa-md-restart": EvidenceGold("section", ((DEPLOY,),)),
    "qa-two-sources": EvidenceGold(
        "page", (_page("depots.pdf", 2) + _page("march-scan.pdf", 1),), "both facts are needed"
    ),
    "figure-handbook": EvidenceGold("figure", (("handbook.docx#figure=1",),)),
    "summarize-release": EvidenceGold(
        "section", ((ROOT, BUILD, DEPLOY),), "a summary of the file should have read every section"
    ),
    "compare-depots-release": EvidenceGold(
        "section", (), "open-ended: no single place is the answer"
    ),
    "review-launch-plan": EvidenceGold(
        "document_block", (("launch-plan#block=4",),), "the one risk the document states"
    ),
    "diagram-release": EvidenceGold("section", ((BUILD, DEPLOY),), "both steps and their owners"),
    "diagram-architecture": EvidenceGold("figure", (("handbook.docx#figure=1",),)),
}


# ---- fixture identity ------------------------------------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture_identity(name: str, data: bytes) -> dict[str, str]:
    """What pins a fixture. PDFs of text and Markdown are byte-deterministic; a scanned PDF and a Word file are not (image
    encoding, zip timestamps), so they are pinned by the content they are generated from: the page texts of the scan, and the
    document XML and media of the Word file."""
    if name in ("depots.pdf", "release.md"):
        return {"kind": "bytes", "sha256": _sha(data)}
    if name == "march-scan.pdf":
        return {
            "kind": "content:page_texts",
            "sha256": _sha(json.dumps(corpus.SCAN_PAGES).encode()),
        }
    if name == "handbook.docx":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = {
                n: z.read(n)
                for n in z.namelist()
                if n == "word/document.xml" or n.startswith("word/media/")
            }
        h = hashlib.sha256()
        for n in sorted(parts):
            h.update(n.encode() + b"\0" + parts[n])
        return {"kind": "content:document_xml+media", "sha256": h.hexdigest()}
    return {"kind": "bytes", "sha256": _sha(data)}


def corpus_identity() -> dict[str, dict[str, str]]:
    return {name: fixture_identity(name, data) for name, data in corpus.sources().items()}


def gold_fingerprint() -> str:
    """One hash over the gold evidence, the strategies and the corpus identity: recorded in every run's manifest."""
    blob = {
        "version": GOLD_VERSION,
        "metric": EVIDENCE_METRIC_VERSION,
        "fixtures": FIXTURE_VERSION,
        "gold": {k: v.to_json() for k, v in sorted(GOLD_EVIDENCE.items())},
        "strategy": STRATEGY_OF_TASK,
        "corpus": corpus_identity(),
        "document_items": [list(i) for i in corpus.DOCUMENT_ITEMS],
    }
    return _sha(json.dumps(blob, sort_keys=True).encode())


# ---- candidates cover units ---------------------------------------------------------------------------------------


def covers(candidate: dict[str, Any]) -> set[str]:
    """The evidence units one candidate covers. `candidate` names its file (`source_name`) and its locator (`page_start`,
    `page_end`, `heading_path`, `figure_number`), or, for the open document, its block index (`block_index`)."""
    out: set[str] = set()
    name = candidate.get("source_name") or ""
    if candidate.get("block_index") is not None:
        out.add(f"{candidate.get('document', 'launch-plan')}#block={candidate['block_index']}")
        return out
    start, end = candidate.get("page_start"), candidate.get("page_end")
    if start:
        out |= {f"{name}#page={p}" for p in range(int(start), int(end or start) + 1)}
    if candidate.get("figure_number"):
        out.add(f"{name}#figure={candidate['figure_number']}")
    path = [str(p) for p in candidate.get("heading_path") or []]
    if path:
        out.add(f"{name}#section={' > '.join(path)}")
    return out


def unit_matches(candidate_units: set[str], unit: str) -> bool:
    """A candidate covers a section unit when its own heading path is that section OR BELOW it (a chunk of `A > B` is evidence
    for `A`). Pages, figures and blocks match exactly."""
    if unit in candidate_units:
        return True
    if "#section=" in unit:
        name, _, path = unit.partition("#section=")
        return any(
            u.startswith(f"{name}#section={path} > ") for u in candidate_units if "#section=" in u
        )
    return False


# ---- the measures --------------------------------------------------------------------------------------------------


def _best_set(gold: EvidenceGold, covered: set[str]) -> tuple[int, int]:
    """(units of the best acceptable set that are covered, size of that set); (0, 0) when there is no gold."""
    best = (0, 0)
    best_share = -1.0
    for s in gold.sets:
        if not s:
            continue
        found = sum(1 for u in s if unit_matches(covered, u))
        share = found / len(s)
        if share > best_share:
            best_share, best = share, (found, len(s))
    return best


def evidence_recall(
    item_id: str, ranked: list[dict[str, Any]], ks: tuple[int, ...] = (1, 3, 5)
) -> dict[str, Any]:
    """Recall@k of gold evidence over RANKED original candidates (best first). Each candidate is a dict `covers()` understands.
    `value` is None when the item has no gold evidence."""
    gold = GOLD_EVIDENCE.get(item_id)
    out: dict[str, Any] = {
        "version": EVIDENCE_METRIC_VERSION,
        "strategy": RANKED,
        "candidates": len(ranked),
        "recall": {},
        "first_hit_rank": None,
    }
    if gold is None or not any(gold.sets):
        out["granularity"] = gold.granularity if gold else None
        out["gold"] = None
        out["recall"] = {f"@{k}": None for k in ks} | {"@all": None}
        return out
    out["granularity"] = gold.granularity
    out["gold"] = [list(s) for s in gold.sets]
    units = [covers(c) for c in ranked]
    for k in (*ks, None):
        covered: set[str] = set().union(*units[:k]) if units else set()
        found, needed = _best_set(gold, covered)
        out["recall"]["@all" if k is None else f"@{k}"] = {
            "found": found,
            "needed": needed,
            "value": found / needed if needed else None,
        }
    for rank in range(1, len(units) + 1):
        covered = set().union(*units[:rank])
        found, needed = _best_set(gold, covered)
        if needed and found:
            out["first_hit_rank"] = rank
            break
    return out


def evidence_coverage(item_id: str, read: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    """What share of the gold evidence the material that was READ covers, for a route that reads whole sections or the whole
    document (no ranking, so no recall@k: this is a different measure and is reported separately)."""
    gold = GOLD_EVIDENCE.get(item_id)
    out: dict[str, Any] = {
        "version": EVIDENCE_METRIC_VERSION,
        "strategy": strategy,
        "candidates": len(read),
        "granularity": gold.granularity if gold else None,
    }
    if gold is None or not any(gold.sets):
        return out | {"gold": None, "coverage": None}
    covered: set[str] = set().union(*[covers(c) for c in read]) if read else set()
    found, needed = _best_set(gold, covered)
    return out | {
        "gold": [list(s) for s in gold.sets],
        "coverage": {"found": found, "needed": needed, "value": found / needed if needed else None},
    }


def summarize_evidence(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-item evidence results of a run. RECALL (ranked items) and COVERAGE (whole-section and whole-document
    items) are kept apart, each with its item count and its pooled units; items without gold are counted, not scored."""
    ranked = [r["evidence"] for r in records if r.get("evidence", {}).get("strategy") == RANKED]
    read = [
        r["evidence"]
        for r in records
        if r.get("evidence", {}).get("strategy") in (FULL_SECTIONS, FULL_DOCUMENT)
    ]
    out: dict[str, Any] = {"version": EVIDENCE_METRIC_VERSION, "recall": {}, "coverage": {}}
    for key in ("@1", "@3", "@5", "@all"):
        got = [e["recall"][key] for e in ranked if e.get("recall", {}).get(key)]
        out["recall"][key] = {
            "items": len(got),
            "mean": (sum(g["value"] for g in got) / len(got)) if got else None,
            "found_units": sum(g["found"] for g in got),
            "needed_units": sum(g["needed"] for g in got),
            "items_all_found": sum(1 for g in got if g["value"] == 1.0),
        }
    cov = [e["coverage"] for e in read if e.get("coverage")]
    out["coverage"] = {
        "items": len(cov),
        "mean": (sum(c["value"] for c in cov) / len(cov)) if cov else None,
        "found_units": sum(c["found"] for c in cov),
        "needed_units": sum(c["needed"] for c in cov),
    }
    out["ranked_items"] = len(ranked)
    out["ranked_items_without_gold"] = sum(1 for e in ranked if e.get("gold") is None)
    out["read_items"] = len(read)
    out["read_items_without_gold"] = sum(1 for e in read if e.get("gold") is None)
    return out


def policies() -> dict[str, str]:
    """The policies above, as recorded in a run manifest."""
    return {
        "unit": "an identifier of a place in an original file (page, section heading path, figure number, document block)",
        "acceptable_sets": "an item lists acceptable sets; the best set's covered share is the item's value",
        "multiple_passages": "a unit covered by several candidates counts once, at the first rank",
        "recall_at_k": "covered units of the best set among the top k ranked candidates / units in that set",
        "aggregate": "mean over items that have gold evidence, with the item count; pooled units reported beside it",
        "missing_gold": "recall None: left out of the aggregate and counted, never 0 or 1",
        "unranked_routes": "summarize and review report coverage of what was read, never recall@k",
        "section_matching": "a candidate under a section path covers that section and every section above it",
        "fixture_identity": "bytes where deterministic, otherwise a hash of the content the fixture is generated from",
    }
