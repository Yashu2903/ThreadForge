# Collaborative documents: Tiptap + Yjs + Hocuspocus

> Status: this file grows with each checkpoint. Sections marked **(cp N)** are written when that
> checkpoint lands; nothing below describes behaviour that does not exist yet.

## 1. Words used here

* **ProseMirror**: the editing library underneath Tiptap. It owns a *schema* (which nodes and marks may
  exist and how they nest) and rejects anything outside it.
* **Tiptap**: a layer on ProseMirror made of *extensions*. Each extension adds nodes, marks, commands or plugins.
* **Yjs**: a CRDT library. A `Y.Doc` is a shared data structure; two people can change it at the same
  time and every copy converges to the same result with no central ordering and no version conflicts.
* **Hocuspocus**: a Node WebSocket server that speaks the Yjs sync protocol and calls our hooks
  (authenticate, load, store).
* **Interchange JSON**: the existing canonical document format (`documents.content`, templates, the
  Python validator, the future DOCX converter). **Tiptap JSON**: the same content in the editor schema.
  `adapt()` converts the first into the second.
* **Seed**: the one-time conversion of a legacy document's interchange JSON into a Yjs document.

## 2. Shared package `packages/editor-schema` (cp 1)

One npm workspace package, `@threadforge/editor-schema`, imported by the browser editor and by the
Node collaboration service, so both always use the same schema. Evidence: `frontend/test/editor-schema.test.ts`
imports it through the Vite alias; both sides compare `schemaFingerprint()` with the pinned
`packages/editor-schema/schema.fingerprint`. Changing any extension changes the fingerprint and fails
both test suites until the file is updated deliberately.

| Export | Purpose |
|---|---|
| `createExtensions(options)`, `getEditorSchema()` | the extension list and the ProseMirror schema built from it |
| `validateDocument(json, limits?)` | structure, limits, silent-drop detection; returns errors with paths |
| `adapt(interchange)` | interchange → Tiptap JSON; throws `AdaptError` with a path on anything unmappable |
| `canonicalize`, `canonicalStringify`, `contentHash`, `blockHash`, `blockHashes` | stable serialization and SHA-256 hashes |
| `extractText`, `normalizeWhitespace` | plain text of a document |
| `FONT_KEYS`, `DEFAULT_LIMITS`, `checkLink`, `normalizeColor` | allowlists and policies |

### 2.1 Which Tiptap extensions are free, and which were written

All `@tiptap/*` packages used are the open-source MIT packages at 3.33.0. **No `@tiptap-pro/*`, no
Tiptap Cloud, no hosted service.** Collaboration extensions are added in checkpoint 3.

| Capability | Source |
|---|---|
| doc, text, paragraph, heading (levels 1–3), bold, italic, underline, strike, hard break, bullet/ordered list | free: `@tiptap/starter-kit` (configured) |
| blockquote (paragraphs only) | free `@tiptap/extension-blockquote`, **content rule overridden** |
| list items (nesting ≤ 3 by validation) | free `@tiptap/extension-list`, **content rule overridden** |
| link (http/https/mailto) | free `@tiptap/extension-link`, **attributes reduced to `href`**, URL policy replaced |
| text style carrier | free `@tiptap/extension-text-style` |
| highlight | free `@tiptap/extension-highlight`, **colour normalized to #RRGGBB, default yellow** |
| superscript / subscript | free `@tiptap/extension-superscript`/`-subscript`, **made mutually exclusive** |
| text alignment | free `@tiptap/extension-text-align` |
| tables | free `@tiptap/extension-table` (+ prosemirror-tables), **cell content restricted, no merge commands exposed, merged cells rejected by validation** |
| stable block ids | free `@tiptap/extension-unique-id` |
| undo/redo | built-in history **disabled** (`undoRedo: false`); Yjs undo is added in checkpoint 3 |
| **ParagraphFormat** (line spacing, space before/after, indent) | **written by us** |
| **TextFormat** (font key from allowlist, size 8–72 pt, text colour) | **written by us** (attributes on the free `textStyle` mark) |
| **AssetImage** (inline image by `assetId`, no `src`) | **written by us** |
| Code, code block, horizontal rule, trailing node | disabled on purpose |

### 2.2 Interchange → Tiptap mapping (`adapt`)

| Interchange | Tiptap JSON |
|---|---|
| `{schema_version:1, blocks}` | `doc` (schema_version must be 1; no blocks → error) |
| `paragraph` `{id, attrs, children}` | `paragraph` `attrs.id` |
| `heading` `{level}` | `heading` `attrs.level` (level 1 omitted: canonical default) |
| `attrs.align` | `attrs.textAlign` |
| `attrs.line_spacing` | `attrs.lineSpacing` |
| `attrs.space_before_pt` / `space_after_pt` | `attrs.spaceBefore` / `attrs.spaceAfter` |
| `attrs.indent_level` | `attrs.indent` |
| `quote` | `blockquote` (children must be paragraphs) |
| `list {ordered}` / `list_item` | `orderedList`/`bulletList` / `listItem` (children: paragraphs and nested lists) |
| `table` / `table_row` | `table` / `tableRow` |
| `table_cell {header}` | `tableCell`, or `tableHeader` when `header: true` |
| `hard_break` | `hardBreak` |
| `image {asset_id, alt, width_px, height_px}` | `image {assetId, alt, width, height}`; **interchange image `id` is not kept** |
| text `marks.bold/italic/underline/strike` | marks `bold`, `italic`, `underline`, `strike` |
| `marks.script` | mark `superscript` or `subscript` |
| `marks.font`, `size_pt`, `color` | one `textStyle` mark `{fontKey, fontSizePt, color}` |
| `marks.highlight` | mark `highlight {color}` |
| `marks.link.href` | mark `link {href}` |
| empty text node | removed |
| any unknown field, type, or mark key | **`AdaptError` with the JSON path** (never dropped silently) |

Output is canonicalized: null and default attributes are omitted and marks are in schema order
(link, textStyle, bold, italic, strike, underline, highlight, superscript, subscript). The `id` of every
block, list item, row and cell is copied from the interchange document.

Golden fixtures: `shared/fixtures/valid/*.json` and `shared/fixtures/canonical/*` (interchange) each have an
expected output in `shared/fixtures/tiptap/` written by an independent Python port of this table.
`packages/editor-schema/test/fixtures.test.ts` checks, for all 13: adapter output equals the expected JSON,
`validateDocument` accepts it, and a ProseMirror load/serialize round trip is identical.

### 2.3 What `validateDocument` checks

1. a walk over the **input JSON** (unknown node/mark types, id format and uniqueness, values and ranges,
   link policy, table shape, list depth, text rules, limits);
2. `schema.nodeFromJSON(json).check()` (ProseMirror's own content rules);
3. **silent-drop detection**: ProseMirror ignores unknown *attributes* while loading. The canonical input is
   compared with the canonical re-serialization; any difference is `would_be_dropped` with a path.

Limits (`DEFAULT_LIMITS`): 1 MiB serialized, 100,000 code points, 5,000 blocks + list items, 60,000 nodes,
100 images, list depth 3, link 2,048 characters, alt text 500, tables 50 × 20, image side ≤ 10,000 px.
Exceeding a limit is an error with the offending path; nothing is truncated.

## 3. Dependency and licence table (cp 1)

Scanned from the installed tree (`node_modules/*/package.json`, hoisted by the npm workspace).

| Package | Version | Licence |
|---|---|---|
| `@tiptap/core`, `/pm`, `/starter-kit`, `/extension-*` (blockquote, highlight, link, list, subscript, superscript, table, text-align, text-style, unique-id) | 3.33.0 | MIT |
| `prosemirror-model` / `-state` / `-view` / `-transform` / `-tables` | 1.25.13 / 1.4.4 / 1.42.9 / 1.12.5 / 1.8.5 | MIT |
| `orderedmap`, `rope-sequence`, `linkifyjs`, `uuid` | 2.1.1, 1.3.4, 4.4.0, 14.0.3 | MIT |
| `vitest`, `typescript`, `@types/node` (dev only) | pinned in `packages/editor-schema/package.json` | MIT / Apache-2.0 |

Whole install tree by licence: MIT (the large majority), Apache-2.0 (8), ISC (7), BSD-3-Clause (2),
BSD-2-Clause (2), MIT-0 (1).

**Flagged: not MIT/BSD/Apache-style:** `caniuse-lite` 1.0.30001823 is **CC-BY-4.0** (a data package pulled in by
browserslist/Vite tooling; it is build-time data, attribution-only and is not shipped in our bundles).
Hocuspocus, Yjs and y-tiptap are added in later checkpoints and will be appended here with their licences.

## 4. Later sections

Architecture and request flows, internal API, close codes, settings, lease, crash-loss window, snapshot
design, awareness caveat, proxy/CSP, operational commands and known limits are written with the
checkpoints that build them (2–6).
