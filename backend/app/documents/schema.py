"""The document model: validation AND canonicalization in one pass.

THE RULE: the validated JSON tree in documents.content is the only editable state. Every write
(editor save, template, DOCX import) goes through validate_document(), which either raises
InvalidDocument (with a JSON-Pointer path to the FIRST problem) or returns the canonical tree.
There is no "lenient" mode and nothing is ever truncated.

The specification is docs/document-schema.md. A TypeScript twin (client/src/document-schema.ts)
implements the same algorithm in the same traversal order; both are run against the golden files in
shared/fixtures/ so they cannot drift apart silently. Keep the two in step.

Canonical form (what we store, hash and send back):
  * keys sorted, no whitespace, UTF-8 (see canonical.py), integral numbers written as integers;
  * attributes equal to their default are omitted (align left, spacing 0, header false...);
  * empty text nodes removed, adjacent text nodes with identical marks merged;
  * an empty paragraph/heading has children: [];
  * uuids lowercase, colours uppercase #RRGGBB, link scheme lowercase.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

DEFAULT_FONT_KEYS: frozenset[str] = frozenset(
    {"sans", "serif", "mono", "calibri", "cambria", "arial", "times_new_roman", "courier_new"}
)

ALIGNS = ("left", "center", "right", "justify")
LINE_SPACINGS = (1.0, 1.15, 1.5, 2.0)
SCRIPTS = ("superscript", "subscript")
LINK_SCHEMES = ("http", "https", "mailto")
MAX_TABLE_ROWS = 50
MAX_TABLE_COLUMNS = 20
MAX_ALT_CODEPOINTS = 500
MAX_IMAGE_PX = 10_000
MAX_INDENT_LEVEL = 8

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


@dataclass(frozen=True)
class Limits:
    """Per-document limits (settings DOCUMENT_MAX_*). Exceeding one is an error, never a trim."""

    max_serialized_bytes: int = 1024 * 1024
    max_text_codepoints: int = 100_000
    max_total_nodes: int = 60_000
    max_blocks: int = 5_000  # block nodes (anywhere) plus list items
    max_inline_nodes: int = 20_000
    max_images: int = 100
    max_list_depth: int = 3
    max_link_length: int = 2048
    allowed_fonts: frozenset[str] = field(default=DEFAULT_FONT_KEYS)


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class DocError:
    path: str  # JSON Pointer into the submitted document ("" is the root)
    code: str
    message: str


class InvalidDocument(Exception):
    """Raised with the first problem found (traversal order is part of the contract)."""

    def __init__(self, path: str, code: str, message: str) -> None:
        super().__init__(f"{path or '/'}: {code}: {message}")
        self.error = DocError(path, code, message)

    @property
    def errors(self) -> list[DocError]:
        return [self.error]


@dataclass(frozen=True)
class ValidatedDocument:
    content: dict[str, Any]  # the canonical tree
    asset_ids: frozenset[str]  # every image asset_id, lowercase
    stats: dict[str, int]


def _fail(path: str, code: str, message: str):
    raise InvalidDocument(path, code, message)


# ---- scalar helpers -----------------------------------------------------------------------------


def is_bad_url_char(ch: str) -> bool:
    """Whitespace, control and invisible characters: never allowed inside a link."""
    o = ord(ch)
    return (
        o <= 0x20
        or 0x7F <= o <= 0xA0
        or o in (0x180E, 0x1680, 0x3000, 0xFEFF)
        or 0x2000 <= o <= 0x200F
        or 0x2028 <= o <= 0x202F
        or 0x205F <= o <= 0x206F
    )


def check_link(href: str, max_length: int) -> str | None:
    """Return the canonical href, or None if the policy rejects it.

    Trim edge whitespace/control characters, then reject any remaining whitespace/control/invisible
    character (browsers strip tabs and newlines inside URLs, which is how "java\\nscript:" slips
    past naive filters). Only http, https and mailto, case-insensitive. http(s) needs "//host" and
    no credentials or backslashes; relative URLs are not links.
    """
    start, end = 0, len(href)
    while start < end and is_bad_url_char(href[start]):
        start += 1
    while end > start and is_bad_url_char(href[end - 1]):
        end -= 1
    s = href[start:end]
    if not s or len(s) > max_length:
        return None
    if any(is_bad_url_char(ch) for ch in s):
        return None
    match = _SCHEME_RE.match(s)
    if match is None:
        return None
    scheme = match.group(1).lower()
    if scheme not in LINK_SCHEMES:
        return None
    rest = s[match.end() :]
    if scheme == "mailto":
        if not rest:
            return None
    else:
        if not rest.startswith("//") or "\\" in s:
            return None
        authority = re.split(r"[/?#]", rest[2:], maxsplit=1)[0]
        if not authority or "@" in authority:
            return None
    return scheme + ":" + rest


def title_error(title: str) -> str | None:
    """Document titles: trimmed (the CALLER trims), 1 to 200 code points, no control or bidi
    override characters. Returns a message or None."""
    if not title or title != title.strip():
        return "must not be empty or have leading/trailing whitespace"
    if len(title) > 200:
        return "must be at most 200 characters"
    for ch in title:
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F or 0xD800 <= o <= 0xDFFF or 0x202A <= o <= 0x202E:
            return "must not contain control characters"
        if 0x2066 <= o <= 0x2069:
            return "must not contain control characters"
    return None


def _num(value: Any) -> int | float:
    """JSON numbers: an integral float becomes an int (2.0 and 2 are the same canonical value)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_integer(value: Any) -> bool:
    return _is_number(value) and float(value).is_integer()


# ---- the walk -----------------------------------------------------------------------------------


class _Walk:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.ids: set[str] = set()
        self.asset_ids: set[str] = set()
        self.text_cp = 0
        self.nodes = 0
        self.blocks = 0
        self.inline = 0
        self.images = 0

    # -- counters ---------------------------------------------------------------------------------
    def node(self, path: str) -> None:
        self.nodes += 1
        if self.nodes > self.limits.max_total_nodes:
            _fail(path, "limit_exceeded", f"more than {self.limits.max_total_nodes} nodes")

    def block(self, path: str) -> None:
        self.node(path)
        self.blocks += 1
        if self.blocks > self.limits.max_blocks:
            _fail(path, "limit_exceeded", f"more than {self.limits.max_blocks} blocks and items")

    # -- shapes -----------------------------------------------------------------------------------
    def obj(self, value: Any, path: str, required: tuple[str, ...], optional: tuple[str, ...]):
        if not isinstance(value, dict):
            _fail(path, "invalid_type", "expected an object")
        for key in value:
            if key not in required and key not in optional:
                _fail(f"{path}/{key}", "unknown_field", "unknown field")
        for key in required:
            if key not in value:
                _fail(f"{path}/{key}", "missing_field", "required field is missing")
        return value

    def array(self, value: Any, path: str, minimum: int = 0) -> list:
        if not isinstance(value, list):
            _fail(path, "invalid_type", "expected an array")
        if len(value) < minimum:
            _fail(path, "invalid_structure", f"needs at least {minimum} item(s)")
        return value

    def kind(self, node: Any, path: str, allowed: tuple[str, ...]) -> str:
        if not isinstance(node, dict):
            _fail(path, "invalid_type", "expected an object")
        t = node.get("type")
        if not isinstance(t, str):
            _fail(f"{path}/type", "missing_field", "type is required")
        if t not in allowed:
            _fail(f"{path}/type", "invalid_structure", f"{t!r} is not allowed here")
        return t

    def uid(self, value: Any, path: str) -> str:
        if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
            _fail(path, "invalid_id", "must be a UUID")
        canonical = value.lower()
        if canonical in self.ids:
            _fail(path, "duplicate_id", "id is used more than once in the document")
        self.ids.add(canonical)
        return canonical

    # -- blocks -----------------------------------------------------------------------------------
    def root(self, raw: Any) -> dict[str, Any]:
        doc = self.obj(raw, "", ("schema_version", "blocks"), ())
        version = doc["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            _fail("/schema_version", "invalid_value", f"must be {SCHEMA_VERSION}")
        blocks = self.array(doc["blocks"], "/blocks", 1)
        out = []
        for i, node in enumerate(blocks):
            path = f"/blocks/{i}"
            t = self.kind(node, path, ("paragraph", "heading", "quote", "list", "table"))
            if t == "paragraph":
                out.append(self.paragraph(node, path, False))
            elif t == "heading":
                out.append(self.heading(node, path))
            elif t == "quote":
                out.append(self.quote(node, path))
            elif t == "list":
                out.append(self.list_(node, path, 1))
            else:
                out.append(self.table(node, path))
        return {"schema_version": SCHEMA_VERSION, "blocks": out}

    def attrs(self, raw: Any, path: str, in_list_item: bool) -> dict[str, Any]:
        if raw is None:
            return {}
        a = self.obj(
            raw,
            path,
            (),
            ("align", "line_spacing", "space_before_pt", "space_after_pt", "indent_level"),
        )
        out: dict[str, Any] = {}
        if "align" in a:
            if a["align"] not in ALIGNS:
                _fail(f"{path}/align", "invalid_value", f"must be one of {', '.join(ALIGNS)}")
            if a["align"] != "left":
                out["align"] = a["align"]
        if "line_spacing" in a:
            v = a["line_spacing"]
            if not _is_number(v) or float(v) not in LINE_SPACINGS:
                _fail(f"{path}/line_spacing", "invalid_value", "must be 1, 1.15, 1.5 or 2")
            if float(v) != 1.0:
                out["line_spacing"] = _num(v)
        for key in ("space_before_pt", "space_after_pt"):
            if key in a:
                v = a[key]
                if not _is_number(v) or not 0 <= v <= 72 or not float(v * 2).is_integer():
                    _fail(f"{path}/{key}", "invalid_value", "must be 0 to 72 in steps of 0.5")
                if v != 0:
                    out[key] = _num(v)
        if "indent_level" in a:
            v = a["indent_level"]
            if not _is_integer(v) or not 0 <= v <= MAX_INDENT_LEVEL:
                _fail(f"{path}/indent_level", "invalid_value", "must be an integer 0 to 8")
            if in_list_item and v != 0:
                _fail(f"{path}/indent_level", "invalid_value", "must be 0 inside list items")
            if v != 0:
                out["indent_level"] = int(v)
        return out

    def paragraph(self, node: dict, path: str, in_list_item: bool) -> dict[str, Any]:
        self.obj(node, path, ("type", "id"), ("attrs", "children"))
        self.block(path)
        out: dict[str, Any] = {"type": "paragraph", "id": self.uid(node["id"], f"{path}/id")}
        attrs = self.attrs(node.get("attrs"), f"{path}/attrs", in_list_item)
        if attrs:
            out["attrs"] = attrs
        children = self.inlines(node.get("children", []), f"{path}/children")
        if children:
            out["children"] = children
        return out

    def heading(self, node: dict, path: str) -> dict[str, Any]:
        self.obj(node, path, ("type", "id", "level"), ("attrs", "children"))
        self.block(path)
        level = node["level"]
        if type(level) is not int or level not in (1, 2, 3):
            _fail(f"{path}/level", "invalid_value", "must be 1, 2 or 3")
        out: dict[str, Any] = {
            "type": "heading",
            "id": self.uid(node["id"], f"{path}/id"),
            "level": level,
        }
        attrs = self.attrs(node.get("attrs"), f"{path}/attrs", False)
        if attrs:
            out["attrs"] = attrs
        children = self.inlines(node.get("children", []), f"{path}/children")
        if children:
            out["children"] = children
        return out

    def quote(self, node: dict, path: str) -> dict[str, Any]:
        self.obj(node, path, ("type", "id", "children"), ())
        self.block(path)
        out_id = self.uid(node["id"], f"{path}/id")
        kids = []
        for i, child in enumerate(self.array(node["children"], f"{path}/children", 1)):
            cpath = f"{path}/children/{i}"
            self.kind(child, cpath, ("paragraph",))
            kids.append(self.paragraph(child, cpath, False))
        return {"type": "quote", "id": out_id, "children": kids}

    def list_(self, node: dict, path: str, depth: int) -> dict[str, Any]:
        self.obj(node, path, ("type", "id", "children"), ("ordered",))
        if depth > self.limits.max_list_depth:
            _fail(path, "limit_exceeded", f"lists nest at most {self.limits.max_list_depth} deep")
        self.block(path)
        out: dict[str, Any] = {"type": "list", "id": self.uid(node["id"], f"{path}/id")}
        if "ordered" in node:
            if type(node["ordered"]) is not bool:
                _fail(f"{path}/ordered", "invalid_type", "expected a boolean")
            if node["ordered"]:
                out["ordered"] = True
        items = []
        for i, item in enumerate(self.array(node["children"], f"{path}/children", 1)):
            items.append(self.list_item(item, f"{path}/children/{i}", depth))
        out["children"] = items
        return out

    def list_item(self, node: Any, path: str, depth: int) -> dict[str, Any]:
        self.kind(node, path, ("list_item",))
        self.obj(node, path, ("type", "id", "children"), ())
        self.block(path)
        out_id = self.uid(node["id"], f"{path}/id")
        kids = []
        for i, child in enumerate(self.array(node["children"], f"{path}/children", 1)):
            cpath = f"{path}/children/{i}"
            if i == 0:
                self.kind(child, cpath, ("paragraph",))
                kids.append(self.paragraph(child, cpath, True))
            else:
                t = self.kind(child, cpath, ("paragraph", "list"))
                if t == "paragraph":
                    kids.append(self.paragraph(child, cpath, True))
                else:
                    kids.append(self.list_(child, cpath, depth + 1))
        return {"type": "list_item", "id": out_id, "children": kids}

    def table(self, node: dict, path: str) -> dict[str, Any]:
        self.obj(node, path, ("type", "id", "children"), ())
        self.block(path)
        out_id = self.uid(node["id"], f"{path}/id")
        rows_raw = self.array(node["children"], f"{path}/children", 1)
        if len(rows_raw) > MAX_TABLE_ROWS:
            _fail(f"{path}/children", "limit_exceeded", f"at most {MAX_TABLE_ROWS} rows")
        rows = []
        width = -1
        for r, row in enumerate(rows_raw):
            rpath = f"{path}/children/{r}"
            self.kind(row, rpath, ("table_row",))
            self.obj(row, rpath, ("type", "id", "children"), ())
            self.node(rpath)
            row_id = self.uid(row["id"], f"{rpath}/id")
            cells_raw = self.array(row["children"], f"{rpath}/children", 1)
            if len(cells_raw) > MAX_TABLE_COLUMNS:
                _fail(f"{rpath}/children", "limit_exceeded", f"at most {MAX_TABLE_COLUMNS} columns")
            if width == -1:
                width = len(cells_raw)
            elif len(cells_raw) != width:
                _fail(
                    f"{rpath}/children", "invalid_structure", "rows must have the same cell count"
                )
            cells = []
            for c, cell in enumerate(cells_raw):
                cells.append(self.cell(cell, f"{rpath}/children/{c}"))
            rows.append({"type": "table_row", "id": row_id, "children": cells})
        return {"type": "table", "id": out_id, "children": rows}

    def cell(self, node: Any, path: str) -> dict[str, Any]:
        self.kind(node, path, ("table_cell",))
        self.obj(node, path, ("type", "id", "children"), ("header",))
        self.node(path)
        out: dict[str, Any] = {"type": "table_cell", "id": self.uid(node["id"], f"{path}/id")}
        if "header" in node:
            if type(node["header"]) is not bool:
                _fail(f"{path}/header", "invalid_type", "expected a boolean")
            if node["header"]:
                out["header"] = True
        kids = []
        for i, child in enumerate(self.array(node["children"], f"{path}/children", 1)):
            cpath = f"{path}/children/{i}"
            self.kind(child, cpath, ("paragraph",))
            kids.append(self.paragraph(child, cpath, False))
        out["children"] = kids
        return out

    # -- inline content ---------------------------------------------------------------------------
    def inlines(self, raw: Any, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i, node in enumerate(self.array(raw, path)):
            npath = f"{path}/{i}"
            t = self.kind(node, npath, ("text", "hard_break", "image"))
            self.node(npath)
            self.inline += 1
            if self.inline > self.limits.max_inline_nodes:
                _fail(
                    npath,
                    "limit_exceeded",
                    f"more than {self.limits.max_inline_nodes} inline nodes",
                )
            if t == "hard_break":
                self.obj(node, npath, ("type",), ())
                out.append({"type": "hard_break"})
            elif t == "image":
                out.append(self.image(node, npath))
            else:
                text = self.text(node, npath)
                if text is None:
                    continue  # empty text nodes disappear
                last = out[-1] if out else None
                if (
                    last is not None
                    and last["type"] == "text"
                    and last.get("marks", {}) == text.get("marks", {})
                ):
                    last["text"] += text["text"]
                else:
                    out.append(text)
        return out

    def text(self, node: dict, path: str) -> dict[str, Any] | None:
        self.obj(node, path, ("type", "text"), ("marks",))
        value = node["text"]
        if not isinstance(value, str):
            _fail(f"{path}/text", "invalid_type", "expected a string")
        if _LONE_SURROGATE_RE.search(value):
            _fail(f"{path}/text", "invalid_text", "contains a lone surrogate")
        for ch in value:
            o = ord(ch)
            if (o < 0x20 and ch != "\t") or 0x7F <= o <= 0x9F or o in (0x2028, 0x2029):
                _fail(
                    f"{path}/text",
                    "invalid_text",
                    "contains a newline or control character (use hard_break for line breaks)",
                )
        self.text_cp += len(value)
        if self.text_cp > self.limits.max_text_codepoints:
            _fail(
                f"{path}/text",
                "limit_exceeded",
                f"more than {self.limits.max_text_codepoints} characters of text",
            )
        marks = self.marks(node.get("marks"), f"{path}/marks")
        if value == "":
            return None
        out: dict[str, Any] = {"type": "text", "text": value}
        if marks:
            out["marks"] = marks
        return out

    def marks(self, raw: Any, path: str) -> dict[str, Any]:
        if raw is None:
            return {}
        m = self.obj(
            raw,
            path,
            (),
            (
                "bold",
                "italic",
                "underline",
                "strike",
                "script",
                "font",
                "size_pt",
                "color",
                "highlight",
                "link",
            ),
        )
        out: dict[str, Any] = {}
        for key in ("bold", "italic", "underline", "strike"):
            if key in m:
                if type(m[key]) is not bool:
                    _fail(f"{path}/{key}", "invalid_type", "expected a boolean")
                if m[key]:
                    out[key] = True
        if "script" in m:
            if m["script"] not in SCRIPTS:
                _fail(f"{path}/script", "invalid_value", "must be superscript or subscript")
            out["script"] = m["script"]
        if "font" in m:
            if not isinstance(m["font"], str) or m["font"] not in self.limits.allowed_fonts:
                _fail(f"{path}/font", "invalid_value", "not an allowed font key")
            out["font"] = m["font"]
        if "size_pt" in m:
            v = m["size_pt"]
            if not _is_number(v) or not 8 <= v <= 72 or not float(v * 2).is_integer():
                _fail(f"{path}/size_pt", "invalid_value", "must be 8 to 72 in steps of 0.5")
            out["size_pt"] = _num(v)
        for key in ("color", "highlight"):
            if key in m:
                if not isinstance(m[key], str) or not _COLOR_RE.fullmatch(m[key]):
                    _fail(f"{path}/{key}", "invalid_value", "must look like #RRGGBB")
                out[key] = m[key].upper()
        if "link" in m:
            link = self.obj(m["link"], f"{path}/link", ("href",), ())
            href = link["href"]
            if not isinstance(href, str):
                _fail(f"{path}/link/href", "invalid_type", "expected a string")
            clean = check_link(href, self.limits.max_link_length)
            if clean is None:
                _fail(
                    f"{path}/link/href",
                    "invalid_link",
                    "only http, https and mailto links are allowed",
                )
            out["link"] = {"href": clean}
        return out

    def image(self, node: dict, path: str) -> dict[str, Any]:
        self.obj(node, path, ("type", "id", "asset_id", "width_px", "height_px"), ("alt",))
        self.images += 1
        if self.images > self.limits.max_images:
            _fail(path, "limit_exceeded", f"more than {self.limits.max_images} images")
        out: dict[str, Any] = {"type": "image", "id": self.uid(node["id"], f"{path}/id")}
        asset = node["asset_id"]
        if not isinstance(asset, str) or not _UUID_RE.fullmatch(asset):
            _fail(f"{path}/asset_id", "invalid_id", "must be a UUID")
        out["asset_id"] = asset.lower()
        self.asset_ids.add(out["asset_id"])
        for key in ("width_px", "height_px"):
            v = node[key]
            if not _is_integer(v) or not 1 <= v <= MAX_IMAGE_PX:
                _fail(f"{path}/{key}", "invalid_value", f"must be an integer 1 to {MAX_IMAGE_PX}")
            out[key] = int(v)
        if "alt" in node:
            alt = node["alt"]
            if not isinstance(alt, str):
                _fail(f"{path}/alt", "invalid_type", "expected a string")
            if _LONE_SURROGATE_RE.search(alt) or any(
                ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or ord(c) in (0x2028, 0x2029) for c in alt
            ):
                _fail(f"{path}/alt", "invalid_text", "contains a control character")
            if len(alt) > MAX_ALT_CODEPOINTS:
                _fail(f"{path}/alt", "limit_exceeded", f"at most {MAX_ALT_CODEPOINTS} characters")
            if alt:
                out["alt"] = alt
        return out


def validate_document(raw: Any, limits: Limits = DEFAULT_LIMITS) -> ValidatedDocument:
    """Validate `raw` (parsed JSON) and return its canonical form, or raise InvalidDocument."""
    from app.documents.canonical import canonical_bytes

    walk = _Walk(limits)
    content = walk.root(raw)
    size = len(canonical_bytes(content))
    if size > limits.max_serialized_bytes:
        _fail(
            "",
            "limit_exceeded",
            f"serialized content is larger than {limits.max_serialized_bytes} bytes",
        )
    return ValidatedDocument(
        content=content,
        asset_ids=frozenset(walk.asset_ids),
        stats={
            "text_codepoints": walk.text_cp,
            "nodes": walk.nodes,
            "blocks": walk.blocks,
            "inline_nodes": walk.inline,
            "images": walk.images,
            "bytes": size,
        },
    )


def new_id() -> str:
    return str(uuid.uuid4())
