"""The human review sheet (tests/eval/build_review_sheet.py): built from committed evidence, human fields left empty.

No provider is called. The sheet is regenerated into a temporary file from `docs/evidence/ai-eval/*.records.json`.
"""

from __future__ import annotations

import re

import pytest

from tests.eval import build_review_sheet as bs


@pytest.fixture(scope="module")
def sheet(tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("sheet") / "sheet.md"
    original = bs.OUT
    bs.OUT = out
    try:
        bs.main()
    finally:
        bs.OUT = original
    return out.read_text(encoding="utf-8")


def _entries(sheet: str) -> dict[str, str]:
    sheet = sheet.split("\n## Appendix A")[0]  # the appendices hold source text, not entries
    parts = re.split(r"^### (\w+) · ", sheet, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_every_entry_has_a_reading_and_every_reading_an_entry():
    ids = {e[0] for e in (*bs.ANSWERS, *bs.PROPOSALS)}
    assert ids == set(bs.AGENT_READING)


def test_each_entry_keeps_the_four_kinds_of_statement_apart(sheet):
    entries = _entries(sheet)
    assert set(entries) == {e[0] for e in (*bs.ANSWERS, *bs.PROPOSALS)}
    for eid, body in entries.items():
        for heading in ("**Automated checks**", "**Agent reading**", "**Human review**"):
            assert body.count(heading) == 1, (eid, heading)
        # the order matters: what the harness checked, then my reading, then the empty human fields
        assert (
            body.index("**Automated checks**")
            < body.index("**Agent reading**")
            < body.index("**Human review**")
        )
        assert "not a human review" in body


def test_the_human_review_fields_are_empty(sheet):
    for eid, body in _entries(sheet).items():
        table = body.split("**Human review**")[1].split("\n\n", 1)[1]
        rows = [
            r
            for r in table.splitlines()
            if r.startswith("|") and not r.startswith("|---") and "Field" not in r
        ]
        assert len(rows) >= 4, eid
        for row in rows:
            assert re.search(r"\|\s*\|\s*$", row), (
                eid,
                row,
            )  # the answer cell after the last pipe is empty


def test_edit_safety_is_asked_for_proposals_only(sheet):
    for eid, body in _entries(sheet).items():
        assert ("Edit safety" in body) == eid.startswith("P"), eid


def test_diagram_release_appears_twice_and_the_repairs_are_real_runs(sheet):
    entries = _entries(sheet)
    assert sum("`diagram-release`" in b for b in entries.values()) == 2
    # a repaired proposal is only listed when the real run recorded a repair
    for eid in ("P7", "P8"):
        assert "valid on the first reply: no" in entries[eid]
        assert "repaired: yes" in entries[eid]


def test_the_sheet_contains_no_secrets_or_local_paths(sheet):
    assert not re.search(
        r"sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,}|[Bb]earer\s+[A-Za-z0-9_\-.]{16,}", sheet
    )
    assert not re.search(r"[A-Za-z]:\\\\?Users|/Users/|/home/", sheet)
