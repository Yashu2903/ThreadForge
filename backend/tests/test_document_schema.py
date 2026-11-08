"""The document validator against the golden fixtures (shared with the TypeScript validator in
client/test/document-schema.test.ts) plus properties the fixtures cannot express."""

import json
import re
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from app.documents.canonical import canonical_json, content_hash
from app.documents.schema import (
    DEFAULT_FONT_KEYS,
    DEFAULT_LIMITS,
    InvalidDocument,
    check_link,
    title_error,
    validate_document,
)
from app.documents.templates import (
    ALL_TEMPLATE_KEYS,
    CREATABLE_TEMPLATES,
    build_template,
    template_document,
)

FIXTURES = Path(__file__).resolve().parents[2] / "shared" / "fixtures"


def load(folder):
    return [
        pytest.param(json.loads(p.read_text(encoding="utf-8")), id=p.stem)
        for p in sorted((FIXTURES / folder).glob("*.json"))
    ]


def limits_from(fixture):
    return replace(DEFAULT_LIMITS, **fixture.get("limits", {}))


def test_fixture_config_matches_the_default_font_allowlist():
    config = json.loads((FIXTURES / "config.json").read_text(encoding="utf-8"))
    assert set(config["fonts"]) == set(DEFAULT_FONT_KEYS)


@pytest.mark.parametrize("fixture", load("valid"))
def test_valid_fixtures_validate_and_are_already_canonical(fixture):
    result = validate_document(fixture["document"])
    assert result.content == fixture["document"]
    assert canonical_json(result.content) == canonical_json(fixture["document"])
    # validating a canonical document again changes nothing (idempotent)
    assert validate_document(result.content).content == result.content


@pytest.mark.parametrize("fixture", load("invalid"))
def test_invalid_fixtures_are_rejected_with_the_expected_path_and_code(fixture):
    with pytest.raises(InvalidDocument) as caught:
        validate_document(fixture["document"], limits_from(fixture))
    error = caught.value.error
    assert (error.path, error.code) == (fixture["error"]["path"], fixture["error"]["code"])
    assert error.message


@pytest.mark.parametrize("fixture", load("canonical"))
def test_canonical_pairs(fixture):
    result = validate_document(fixture["input"])
    assert result.content == fixture["output"]
    assert canonical_json(result.content) == canonical_json(fixture["output"])
    assert validate_document(result.content).content == result.content


def test_there_are_enough_fixtures_to_mean_something():
    assert len(load("valid")) >= 8 and len(load("invalid")) >= 50 and len(load("canonical")) >= 5


# ---- canonicalization properties ----------------------------------------------------------------


def test_canonical_json_ignores_key_order_and_number_spelling():
    a = {"b": 2.0, "a": [1, {"y": 1.5, "x": True}]}
    b = {"a": [1, {"x": True, "y": 1.5}], "b": 2}
    assert canonical_json(a) == canonical_json(b) == '{"a":[1,{"x":true,"y":1.5}],"b":2}'
    assert content_hash(a) == content_hash(b) and len(content_hash(a)) == 32


def test_canonical_json_keeps_non_ascii_unescaped_utf8():
    assert canonical_json({"t": "é\u2028😀"}) == '{"t":"é\u2028😀"}'


def test_the_same_content_in_a_different_spelling_hashes_equal():
    base = {
        "schema_version": 1,
        "blocks": [
            {
                "type": "paragraph",
                "id": str(uuid.uuid4()),
                "children": [{"type": "text", "text": "a"}],
            }
        ],
    }
    split = json.loads(json.dumps(base))
    split["blocks"][0]["children"] = [{"type": "text", "text": "a"}, {"type": "text", "text": ""}]
    assert content_hash(validate_document(base).content) == content_hash(
        validate_document(split).content
    )


def test_nan_and_infinity_are_rejected():
    for bad in (float("nan"), float("inf")):
        raw = {
            "schema_version": 1,
            "blocks": [
                {"type": "paragraph", "id": str(uuid.uuid4()), "attrs": {"space_after_pt": bad}}
            ],
        }
        with pytest.raises(InvalidDocument):
            validate_document(raw)


def test_booleans_are_not_numbers():
    raw = {
        "schema_version": 1,
        "blocks": [{"type": "paragraph", "id": str(uuid.uuid4()), "attrs": {"indent_level": True}}],
    }
    with pytest.raises(InvalidDocument):
        validate_document(raw)
    raw["schema_version"] = True
    with pytest.raises(InvalidDocument):
        validate_document(raw)


def test_asset_ids_are_collected_lowercase():
    asset = str(uuid.uuid4())
    raw = {
        "schema_version": 1,
        "blocks": [
            {
                "type": "paragraph",
                "id": str(uuid.uuid4()),
                "children": [
                    {
                        "type": "image",
                        "id": str(uuid.uuid4()),
                        "asset_id": asset.upper(),
                        "width_px": 10,
                        "height_px": 10,
                    }
                ],
            }
        ],
    }
    result = validate_document(raw)
    assert result.asset_ids == {asset}
    assert result.stats["images"] == 1


# ---- links --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://example.com", "https://example.com"),
        ("HTTP://EXAMPLE.com/x", "http://EXAMPLE.com/x"),
        ("  mailto:a@b.co  ", "mailto:a@b.co"),
        ("https://example.com/path?q=1#frag", "https://example.com/path?q=1#frag"),
        ("https://例え.jp/パス", "https://例え.jp/パス"),
    ],
)
def test_links_that_pass(href, expected):
    assert check_link(href, 2048) == expected


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "jav\tascript:alert(1)",
        "java\x00script:alert(1)",
        " \x01javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html;base64,AAAA",
        "vbscript:msgbox",
        "file:///c:/windows",
        "ftp://example.com",
        "//example.com",
        "https:example.com",
        "https://exa\u00a0mple.com",
        "https://example.com/\u2028x",
        "https://evil.com\\@good.com",
        "https://user@example.com",
        "",
        "   ",
        "mailto:",
    ],
)
def test_links_that_are_refused(href):
    assert check_link(href, 2048) is None


def test_link_length_is_checked_after_trimming():
    ok = "https://e.com/" + "a" * (2048 - len("https://e.com/"))
    assert check_link("  " + ok + "  ", 2048) == ok
    assert check_link(ok + "a", 2048) is None


# ---- titles -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title", ["Plan", "日本語のタイトル", "a" * 200, "emoji 😀", "zwj 👨\u200d👩\u200d👧"]
)
def test_titles_that_pass(title):
    assert title_error(title) is None


@pytest.mark.parametrize(
    "title",
    ["", " pad", "pad ", "a" * 201, "tab\there", "nul\x00", "line\nbreak", "\u202eevil", "\ud800"],
)
def test_titles_that_fail(title):
    assert title_error(title) is not None


# ---- limits with real sizes ---------------------------------------------------------------------


def paragraphs(count, text="x"):
    return {
        "schema_version": 1,
        "blocks": [
            {
                "type": "paragraph",
                "id": str(uuid.uuid4()),
                "children": [{"type": "text", "text": text}] if text else [],
            }
            for _ in range(count)
        ],
    }


def test_default_limits_match_the_spec():
    assert DEFAULT_LIMITS.max_serialized_bytes == 1024 * 1024
    assert DEFAULT_LIMITS.max_text_codepoints == 100_000
    assert DEFAULT_LIMITS.max_total_nodes == 60_000
    assert DEFAULT_LIMITS.max_blocks == 5_000
    assert DEFAULT_LIMITS.max_inline_nodes == 20_000
    assert DEFAULT_LIMITS.max_images == 100
    assert DEFAULT_LIMITS.max_list_depth == 3
    assert DEFAULT_LIMITS.max_link_length == 2048


def test_exactly_5000_blocks_pass_and_5001_fail_with_a_path():
    assert validate_document(paragraphs(5000, "")).stats["blocks"] == 5000
    with pytest.raises(InvalidDocument) as caught:
        validate_document(paragraphs(5001, ""))
    assert (caught.value.error.path, caught.value.error.code) == ("/blocks/5000", "limit_exceeded")


def test_text_limit_is_counted_in_code_points_not_bytes():
    emoji = "😀" * 100_000  # 100,000 code points, 400,000 UTF-8 bytes
    assert validate_document(paragraphs(1, emoji)).stats["text_codepoints"] == 100_000
    with pytest.raises(InvalidDocument) as caught:
        validate_document(paragraphs(1, emoji + "a"))
    assert caught.value.error.code == "limit_exceeded"


def test_serialized_size_is_counted_in_utf8_bytes():
    # 100,000 emoji code points are under the text limit but over 1 MiB once serialized
    with pytest.raises(InvalidDocument) as caught:
        validate_document(
            paragraphs(1, "😀" * 100_000), replace(DEFAULT_LIMITS, max_serialized_bytes=300_000)
        )
    assert (caught.value.error.path, caught.value.error.code) == ("", "limit_exceeded")


def test_inline_node_limit():
    many = {
        "schema_version": 1,
        "blocks": [
            {
                "type": "paragraph",
                "id": str(uuid.uuid4()),
                "children": [{"type": "hard_break"}] * 20_001,
            }
        ],
    }
    with pytest.raises(InvalidDocument) as caught:
        validate_document(many)
    assert caught.value.error.path == "/blocks/0/children/20000"


def test_table_size_limits():
    def table(rows, cols):
        return {
            "schema_version": 1,
            "blocks": [
                {
                    "type": "table",
                    "id": str(uuid.uuid4()),
                    "children": [
                        {
                            "type": "table_row",
                            "id": str(uuid.uuid4()),
                            "children": [
                                {
                                    "type": "table_cell",
                                    "id": str(uuid.uuid4()),
                                    "children": [{"type": "paragraph", "id": str(uuid.uuid4())}],
                                }
                                for _ in range(cols)
                            ],
                        }
                        for _ in range(rows)
                    ],
                }
            ],
        }

    assert validate_document(table(50, 20)).stats["blocks"] == 1 + 50 * 20
    with pytest.raises(InvalidDocument) as caught:
        validate_document(table(51, 1))
    assert caught.value.error.code == "limit_exceeded"
    with pytest.raises(InvalidDocument) as caught:
        validate_document(table(1, 21))
    assert caught.value.error.code == "limit_exceeded"


def test_hostile_depth_is_bounded_by_the_schema_not_by_recursion():
    # a deeply nested object where a text node should be is rejected at the first unknown field
    nested: dict = {"type": "text", "text": "x"}
    for _ in range(5000):
        nested = {"type": "text", "text": "x", "marks": {"link": nested}}
    raw = {
        "schema_version": 1,
        "blocks": [{"type": "paragraph", "id": str(uuid.uuid4()), "children": [nested]}],
    }
    with pytest.raises(InvalidDocument) as caught:
        validate_document(raw)
    assert caught.value.error.code == "unknown_field"


# ---- templates ----------------------------------------------------------------------------------


def test_every_template_validates_and_is_already_canonical():
    for key in CREATABLE_TEMPLATES:
        raw = build_template(key)
        result = validate_document(raw)
        assert result.content == raw, key
        assert template_document(key, DEFAULT_LIMITS).content["schema_version"] == 1


def test_templates_have_unique_fresh_ids_each_time():
    first = {n["id"] for n in build_template("project_brief")["blocks"]}
    second = {n["id"] for n in build_template("project_brief")["blocks"]}
    assert len(first) == 8 and not (first & second)


def test_template_headings_are_exactly_the_specified_sections():
    expected = {
        "blank": [],
        "project_brief": ["Objective", "Scope", "Requirements", "Next steps"],
        "technical_proposal": [
            "Problem",
            "Proposed approach",
            "Architecture",
            "Tradeoffs",
            "Implementation plan",
        ],
        "meeting_notes": ["Agenda", "Discussion", "Decisions", "Action items"],
        "research_summary": [
            "Research question",
            "Findings",
            "Evidence",
            "Limitations",
            "Conclusions",
        ],
    }
    for key, headings in expected.items():
        blocks = build_template(key)["blocks"]
        found = [b["children"][0]["text"] for b in blocks if b["type"] == "heading"]
        assert found == headings, key
        assert all(b["type"] in ("heading", "paragraph") for b in blocks)
        assert all("children" not in b for b in blocks if b["type"] == "paragraph")  # empty


def test_template_keys_include_imported_but_it_cannot_be_created_directly():
    assert "imported" in ALL_TEMPLATE_KEYS and "imported" not in CREATABLE_TEMPLATES
    with pytest.raises(ValueError):
        build_template("imported")


def test_fixture_files_are_utf8_json_with_no_stray_secrets():
    for path in FIXTURES.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        assert not re.search(r"(?i)password|secret|token", text), path
