"""Phase 2: UTF-16 ranges (shared cases with the TypeScript package) and the resolution of an edit target."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.ai.answer.capture import Captured, CaptureFailed, DocBlock, selection_hash
from app.ai.edit.target import END, START, linearize, resolve_edit_target
from app.ai.edit.units import (
    OBJECT_REPLACEMENT,
    RangeError,
    block_units,
    text_hash,
    utf16_length,
    utf16_slice,
)

CASES = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "editor-schema"
        / "test"
        / "fixtures"
        / "range-cases.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_ranges_match_the_typescript_twin(case):
    units = block_units(case["block"])
    assert units == case["units"]
    for start, end, expected in case["slices"]:
        if expected is None:
            with pytest.raises(RangeError):
                utf16_slice(units, start, end)
        else:
            assert utf16_slice(units, start, end) == expected


def test_lengths_and_hashes():
    assert utf16_length("a😀b") == 4
    assert utf16_length(OBJECT_REPLACEMENT) == 1
    assert text_hash("abc") == hashlib.sha256(b"abc").hexdigest()


def test_a_container_has_no_text():
    with pytest.raises(RangeError):
        block_units({"type": "bulletList", "attrs": {"id": "x"}})


# ---- a small captured document -------------------------------------------------------------------------


def _id() -> str:
    return str(uuid.uuid4())


def build() -> Captured:
    ids = {name: _id() for name in ("h", "p1", "p2", "ul", "li", "li_p", "d", "p3", "p4")}

    def para(key: str, text: str) -> dict[str, Any]:
        return {
            "type": "paragraph",
            "attrs": {"id": ids[key]},
            "content": [{"type": "text", "text": text}],
        }

    content = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"id": ids["h"], "level": 2},
                "content": [{"type": "text", "text": "Plan"}],
            },
            para("p1", "First paragraph about 😀 emoji."),
            para("p2", "Second paragraph."),
            {
                "type": "bulletList",
                "attrs": {"id": ids["ul"]},
                "content": [
                    {
                        "type": "listItem",
                        "attrs": {"id": ids["li"]},
                        "content": [para("li_p", "list text")],
                    }
                ],
            },
            {
                "type": "diagram",
                "attrs": {"id": ids["d"]},
                "content": [{"type": "text", "text": "flowchart TD\n  A --> B"}],
            },
            para("p3", "Third paragraph."),
            para("p4", "Last."),
        ],
    }
    blocks: list[DocBlock] = []

    def walk(nodes: list[dict[str, Any]], parent: str | None) -> None:
        for node in nodes:
            bid = node["attrs"]["id"]
            text = "".join(c.get("text", "") for c in node.get("content", []) if "text" in c)
            digest = hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()
            level = node["attrs"].get("level", 0) if node["type"] == "heading" else 0
            blocks.append(DocBlock(bid, node["type"], level, text, digest, parent))
            walk([c for c in node.get("content", []) if "attrs" in c], bid)

    walk(content["content"], None)
    cap = Captured(uuid.uuid4(), 7, "c" * 64, blocks, content)
    cap.ids = ids  # type: ignore[attr-defined]
    return cap


def ids_of(cap: Captured) -> dict[str, str]:
    return cap.ids  # type: ignore[attr-defined]


def sel_hash(cap: Captured, *keys: str) -> str:
    by_id = {b.id: b for b in cap.blocks}
    order = {b.id: i for i, b in enumerate(cap.blocks)}
    chosen = sorted((by_id[ids_of(cap)[k]] for k in keys), key=lambda b: order[b.id])
    return selection_hash(chosen)


def test_no_target_is_refused_not_guessed():
    cap = build()
    with pytest.raises(CaptureFailed) as exc:
        resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[], insert_at=None)
    assert exc.value.code == "target_required"


def test_selected_blocks_scope_baseline_and_before_copy():
    cap = build()
    i = ids_of(cap)
    target = resolve_edit_target(
        cap,
        block_ids=[i["p2"], i["ul"]],
        selection_hash=sel_hash(cap, "p2", "ul"),
        ranges=[],
        insert_at=None,
    )
    assert target.block_ids == [i["p2"], i["ul"]]
    # the list brings its item and the paragraph inside it
    assert target.scope_ids == {i["p2"], i["ul"], i["li"], i["li_p"]}
    assert set(target.base_hashes) == target.scope_ids
    by_id = {b.id: b for b in cap.blocks}
    assert all(target.base_hashes[k] == by_id[k].hash for k in target.base_hashes)
    assert set(target.before) == {i["p2"], i["ul"]}
    assert (
        target.before[i["ul"]]["content"][0]["attrs"]["id"] == i["li"]
    )  # children are in the copy
    assert target.counter == 7
    # anchors are TOP-LEVEL blocks: the list itself and the paragraph
    assert target.anchors == [i["p2"], i["ul"]]


def test_a_nested_selection_anchors_on_its_top_level_block():
    cap = build()
    i = ids_of(cap)
    target = resolve_edit_target(
        cap,
        block_ids=[i["li_p"]],
        selection_hash=sel_hash(cap, "li_p"),
        ranges=[],
        insert_at=None,
    )
    assert target.anchors == [i["ul"]]


def test_a_changed_selection_is_selection_changed():
    cap = build()
    i = ids_of(cap)
    good = sel_hash(cap, "p2")
    cap.blocks[2] = DocBlock(
        cap.blocks[2].id, "paragraph", 0, "edited", "f" * 64, cap.blocks[2].parent_id
    )
    with pytest.raises(CaptureFailed) as exc:
        resolve_edit_target(
            cap, block_ids=[i["p2"]], selection_hash=good, ranges=[], insert_at=None
        )
    assert exc.value.code == "selection_changed"
    with pytest.raises(CaptureFailed) as exc:
        resolve_edit_target(
            cap, block_ids=[str(uuid.uuid4())], selection_hash=good, ranges=[], insert_at=None
        )
    assert exc.value.code == "selection_changed"


def range_of(cap: Captured, key: str, start: int, end: int, *, text: str | None = None):
    node = next(n for n in cap.content["content"] if n["attrs"]["id"] == ids_of(cap)[key])
    real = utf16_slice(block_units(node), start, end)
    return {
        "block_id": ids_of(cap)[key],
        "start": start,
        "end": end,
        "text_hash": text_hash(real if text is None else text),
    }


def test_a_range_resolves_with_utf16_units_around_an_emoji():
    cap = build()
    i = ids_of(cap)
    # "First paragraph about 😀 emoji." : the emoji is units 22-24
    r = range_of(cap, "p1", 22, 24)
    target = resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[r], insert_at=None)
    assert target.ranges[0].block_id == i["p1"]
    assert target.range_text[f"{i['p1']}:22:24"] == "😀"
    assert target.scope_ids == {i["p1"]}
    assert target.anchors == [i["p1"]]
    assert target.before[i["p1"]]["type"] == "paragraph"


@pytest.mark.parametrize(
    "start,end",
    [(23, 25), (22, 23), (5, 5), (0, 400), (-1, 3)],
    ids=["splits-pair-end", "splits-pair-mid", "empty", "past-end", "negative"],
)
def test_bad_ranges_fail_as_selection_changed(start, end):
    cap = build()
    i = ids_of(cap)
    raw = {"block_id": i["p1"], "start": start, "end": end, "text_hash": "0" * 64}
    with pytest.raises(CaptureFailed) as exc:
        resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[raw], insert_at=None)
    assert exc.value.code == "selection_changed"


def test_a_range_whose_text_changed_is_selection_changed():
    cap = build()
    r = range_of(cap, "p2", 0, 6, text="Other ")
    with pytest.raises(CaptureFailed) as exc:
        resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[r], insert_at=None)
    assert exc.value.code == "selection_changed"


def test_overlapping_ranges_and_container_ranges_are_refused():
    cap = build()
    a, b = range_of(cap, "p2", 0, 6), range_of(cap, "p2", 3, 9)
    with pytest.raises(CaptureFailed):
        resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[a, b], insert_at=None)
    raw = {"block_id": ids_of(cap)["ul"], "start": 0, "end": 1, "text_hash": "0" * 64}
    with pytest.raises(CaptureFailed):
        resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[raw], insert_at=None)


def test_insert_at_start_and_end_need_no_selection():
    cap = build()
    i = ids_of(cap)
    start = resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[], insert_at=START)
    assert start.anchors == [START] and start.scope_ids == set()
    end = resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[], insert_at=END)
    assert end.anchors == [i["p4"]]
    with pytest.raises(CaptureFailed):
        resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[], insert_at="middle")


def test_a_diagram_can_be_a_range_target():
    cap = build()
    i = ids_of(cap)
    r = range_of(cap, "d", 0, 9)
    target = resolve_edit_target(cap, block_ids=[], selection_hash=None, ranges=[r], insert_at=None)
    assert target.range_text[f"{i['d']}:0:9"] == "flowchart"


def test_the_model_sees_labels_not_ids_and_only_the_scope():
    cap = build()
    i = ids_of(cap)
    target = resolve_edit_target(
        cap,
        block_ids=[i["p2"]],
        selection_hash=sel_hash(cap, "p2"),
        ranges=[],
        insert_at=None,
    )
    view = linearize(cap, target, toc_when_blocks_over=3)
    assert view.scope_lines == ["[B3] (paragraph) Second paragraph."]
    assert view.labels["B3"] == i["p2"]
    assert view.toc_lines == ["[B1] (heading 2) Plan"]
    assert view.anchor_labels == ["B3"]
    assert i["p2"] not in "\n".join([*view.scope_lines, *view.toc_lines])
    # a small document needs no table of contents
    assert linearize(cap, target).toc_lines == []
