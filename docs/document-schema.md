# ThreadForge document schema (version 1)

The document is a JSON tree. It is the **only** editable state of a document: it is stored in
`documents.content` (PostgreSQL JSONB), validated on every write by the same rules in two
implementations that are tested against the same golden files:

| implementation | file |
|---|---|
| Python (server) | `backend/app/documents/schema.py` |
| TypeScript (editor, client) | `client/src/document-schema.ts` |
| golden fixtures | `shared/fixtures/{valid,invalid,canonical}/*.json`, `shared/fixtures/config.json` |

Both validators **canonicalize while they validate**: the output is the canonical tree. The editor
canonicalizes before saving, so the server normally changes nothing; if it does, the save response
says `canonical_changed: true` and carries the canonical content.

## Shape

```
document   { schema_version: 1, blocks: [block, ...] }        at least one block
block      paragraph | heading | quote | list | table
paragraph  { type, id, attrs?, children?: [inline] }
heading    { type, id, level: 1|2|3, attrs?, children?: [inline] }
quote      { type, id, children: [paragraph, ...] }            one or more
list       { type, id, ordered?: true, children: [list_item, ...] }
list_item  { type, id, children: [paragraph, (paragraph | list)...] }   first child is a paragraph
table      { type, id, children: [table_row, ...] }            1..50 rows, rectangular
table_row  { type, id, children: [table_cell, ...] }           1..20 cells, same count in every row
table_cell { type, id, header?: true, children: [paragraph, ...] }      paragraphs only
inline     text | hard_break | image
text       { type: "text", text, marks? }
hard_break { type: "hard_break" }
image      { type: "image", id, asset_id, width_px, height_px, alt? }
```

* **ids**: every block, list item, table row, table cell and image has a UUID `id`, unique across
  the whole document (a duplicate is rejected; ids need not be server-issued). Text and hard breaks
  have no id. Canonical form is lowercase.
* **Nesting**: lists nest at most **3** deep (a list directly in the document is depth 1). Lists occur
  only at the top level or inside list items; quotes and cells hold paragraphs only.
* **Paragraph attrs** (all optional, defaults omitted): `align` (`left` default, `center`, `right`,
  `justify`); `line_spacing` (`1` default, `1.15`, `1.5`, `2`); `space_before_pt`, `space_after_pt`
  (0 to 72, step 0.5; 0 default); `indent_level` (0 to 8 integer; one level is 36 pt; must be 0
  inside list items, because list nesting already indents).
* **Marks** (an object on a text node, all optional): `bold`, `italic`, `underline`, `strike`
  (true; `false` is accepted and dropped); `script` (`superscript` or `subscript`, mutually
  exclusive because it is one field); `font` (a key from the server allowlist, never CSS);
  `size_pt` (8 to 72, step 0.5); `color`, `highlight` (`#RRGGBB`, normalized to uppercase);
  `link` `{ href }` (policy below). Canonical mark order is irrelevant: the serialization sorts keys.
* **Text**: Unicode with **no newline characters** (a line break is a `hard_break`), tab allowed, no
  other control characters (C0, DEL, C1, U+2028, U+2029, NUL), no lone surrogates. Whitespace is
  preserved exactly (render with `white-space: pre-wrap`). Format characters such as the zero-width
  joiner are allowed, because emoji sequences need them.
* **Image**: `alt` up to 500 code points (default empty, omitted); `width_px`/`height_px` integers
  1 to 10,000; `asset_id` a UUID of an asset of the same room (checked at save time).

## Link policy

After trimming leading/trailing whitespace, control and invisible characters (U+0000 to U+0020,
U+007F to U+00A0, U+1680, U+180E, U+2000 to U+200F, U+2028 to U+202F, U+205F to U+206F, U+3000,
U+FEFF):

1. any remaining such character anywhere in the URL rejects it (browsers strip tabs and newlines
   inside URLs, which is how `java\nscript:` slips past naive filters);
2. at most 2048 code points;
3. a scheme is required and must be `http`, `https` or `mailto`, case-insensitive (so
   `javascript:`, `data:`, `vbscript:`, `file:`, `ftp:`, `//host` and relative URLs are rejected);
4. `http`/`https` need `//` and a non-empty host, with no `@` (credentials in a link are a phishing
   tool) and no backslash; `mailto:` needs something after the colon;
5. the canonical href has a lowercase scheme and is otherwise unchanged.

Rendering uses `rel="noopener noreferrer"` and `target="_blank"`. In the editor a link opens only on
Ctrl/Cmd-click or from the link popover.

## Canonical form

* object keys sorted; no whitespace; UTF-8 (not `\u`-escaped); an integral number is written as an
  integer (`2.0` becomes `2`);
* attributes equal to their default are omitted (`align: left`, spacing `0`, `bold: false`,
  `header: false`, `ordered: false`, empty `alt`);
* empty text nodes are removed; adjacent text nodes with identical marks are merged;
* an empty paragraph or heading has no `children`;
* unknown types, attributes and marks are **rejected**, never ignored.

`content_hash` is the SHA-256 of the canonical serialization, so "is this the same content?" is a
hash comparison and never depends on JSONB key order.

**Known limitation:** formatting of an *empty* paragraph is not persisted (a bold caret in an empty
paragraph is editor UI state only), because an empty paragraph has no text node to carry marks.

## Limits (settings, defaults)

| setting | default | counted |
|---|---|---|
| `DOCUMENT_MAX_SERIALIZED_BYTES` | 1 MiB | UTF-8 bytes of the canonical serialization |
| `DOCUMENT_MAX_TEXT_CODEPOINTS` | 100,000 | code points over all text nodes |
| `DOCUMENT_MAX_TOTAL_NODES` | 60,000 | blocks, items, rows, cells and inline nodes |
| `DOCUMENT_MAX_BLOCKS` | 5,000 | block nodes anywhere (paragraph, heading, quote, list, table) plus list items |
| `DOCUMENT_MAX_INLINE_NODES` | 20,000 | text, hard_break and image nodes |
| `DOCUMENT_MAX_IMAGES` | 100 | images |
| `DOCUMENT_MAX_LIST_DEPTH` | 3 | list nesting |
| `DOCUMENT_MAX_LINK_LENGTH` | 2048 | code points |

Table limits (50 rows, 20 columns) and the alt-text limit (500) are fixed by the schema. Exceeding a
limit is an error with a path; nothing is ever truncated.

## Errors

The validator reports the **first** problem in a fixed traversal order (the order is part of the
contract; the golden files check it). An error is `{ path, code, message }`:

* `path`: a JSON Pointer into the submitted document (`""` is the document itself);
* `code`: `invalid_type`, `missing_field`, `unknown_field`, `invalid_value`, `invalid_structure`,
  `invalid_id`, `duplicate_id`, `invalid_text`, `invalid_link`, `limit_exceeded`;
* `message`: a fixed string. Submitted text is never echoed (so a rejected link or paragraph does not
  leak into logs or responses).

Over HTTP this is `422 invalid_document_content` with `error.errors: [ {path, code, message} ]`.

## Fonts

`font` is a key, never a CSS string. Default keys (setting `DOCUMENT_FONT_KEYS`): `sans`, `serif`,
`mono`, `calibri`, `cambria`, `arial`, `times_new_roman`, `courier_new`. The browser maps each key to
a self-hosted, openly licensed, metric-compatible font (Carlito for Calibri, Caladea for Cambria,
Arimo for Arial, Tinos for Times New Roman, Cousine for Courier New). Fonts we do not bundle may
look different on another machine; the editor shows a notice where that matters.

## Differences between the two implementations

JSON has one number type in JavaScript, so `1.0` and `1` are the same value there. The Python
validator rejects `schema_version: 1.0` and `level: 2.0` (it sees a float); TypeScript cannot tell.
The editor never produces such values (`JSON.stringify` writes `1`), and the server is the
authority. Everything else in the fixtures behaves identically.
