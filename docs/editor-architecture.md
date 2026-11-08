# Editor architecture

ThreadForge's editor is our own code (no Tiptap, ProseMirror, Lexical, Slate, Quill, CKEditor or
TinyMCE). Browser primitives are used: `beforeinput`, `Selection`, `Range`, `Intl.Segmenter`,
`crypto.randomUUID`. No dependency was added for the editor.

```
frontend/src/editor/
  doc.ts       immutable document tree + id index + updateNode / removeNode (structural sharing)
  inline.ts    operations on a text block's inline sequence (slice, delete, insert, marks)
  segment.ts   grapheme / word boundaries (Intl.Segmenter, with a documented fallback)
  commands.ts  PURE editing commands: (document, selection, args) -> new document + selection
  stats.ts     cheap counters to refuse an edit that would break a limit
  history.ts   undo / redo over document snapshots
  dom.ts       renderer, DOM <-> model position mapping, reading a block back from the DOM
  editor.ts    the island: events in, commands run, DOM updated, history recorded
  EditorSurface.tsx   the React host: ONE empty <div>; React never renders inside it
```

## Layers and what each one may know

* **model** (`doc.ts`, `inline.ts`): plain immutable JSON in the canonical schema. No DOM.
* **commands** (`commands.ts`): pure functions. Easy to test, impossible to break the DOM.
* **renderer** (`dom.ts`): turns the model into DOM and patches only blocks whose object changed.
* **selection mapping** (`dom.ts`): the rules below.
* **history** (`history.ts`): snapshots (pointers to immutable documents).
* **persistence**: not in the editor. The host listens to `onChange` and saves (checkpoint 6).

## Why model-first, and what happens when the browser disagrees

For every input type we support the editor cancels `beforeinput`, changes the model, and updates
the DOM itself, so the browser never mutates the DOM and the two cannot drift. Unknown types
(`formatBold`, `insertFromDrop`, `insertTranspose`, anything new) are cancelled too: nothing may
edit the DOM silently. `document.execCommand` is not used.

Safety nets:

1. After each command the blocks at the caret are compared with the model (`domText` vs `flatten`);
   a mismatch redraws the block from the model and logs `[editor] DOM drifted…` in development.
   `Editor.findDrift()` checks every block (tests and Playwright call it).
2. An `input` event only reaches the editor if some `beforeinput` was not cancelled (an engine quirk,
   an autofill). The block is read back from the DOM into the model and the event is counted.

### IME composition

From `compositionstart` to `compositionend` the browser owns the DOM (an IME breaks if you touch the
text under it). The editor therefore: does not cancel `beforeinput` (`isComposing` or
`insertCompositionText`); makes **no model commit, no history entry and no change event** (so no
autosave); and exposes `isComposing`. At `compositionend`, after a `setTimeout(0)` (Chromium lands the
final mutation just after the event, Safari just before), it reads the touched blocks back
(`readInlines`), commits ONE history entry (`kind: "composition"`), and redraws from the model.
Marks survive because each marked run carries `data-m` (its marks as JSON).
Real IME behaviour in Safari and with Japanese/Chinese input is checked by hand:
[editor-ime-checklist.md](editor-ime-checklist.md). Chromium's composition is simulated in
Playwright with `Input.imeSetComposition`.

## Positions and the DOM <-> model mapping

A position is `{ id, offset }`: `id` names a **text block** (paragraph or heading; ids are unique in
the document, so the block id path of the spec is derived from the index: `indexOf(doc).chain(id)`).

`offset` counts **UTF-16 code units of text, plus 1 for each inline atom** (hard break, image). So
`"a😀b"` has length 4 and an atom between two letters adds one. `flatten()` produces a string where
each atom is U+FFFC, so string offsets and model offsets are the same numbers.

DOM structure (`dom.ts` header documents the same rules):

| model | DOM |
|---|---|
| paragraph, heading | `<p>`, `<h1>`–`<h3>` with `data-id` and `data-kind="textblock"` |
| quote, list, list item, table, row, cell | `blockquote`, `ul`/`ol`, `li`, `table>tbody`, `tr`, `td`/`th`, each with `data-id` |
| text without marks | a DOM Text node |
| text with marks | wrapper(s) `strong/em/u/s/sup/sub/span/a`; the outermost has `data-m` = marks JSON; the Text node is innermost |
| hard break | `<br data-atom="hard_break">` |
| image | `<img data-atom="image" data-id data-asset contenteditable="false">` |
| empty block, or block ending in a hard break | one extra `<br data-filler>` (length 0) |

* DOM point -> position: find the enclosing text block element; the offset is the summed length of
  everything before the point (Text = `data.length`, atom = 1, filler = 0). A point on an element
  boundary counts the children before it. A point in a container that is not a text block (between
  table rows, in the root) maps to the start of the next text block, else the end of the previous one.
* Position -> DOM point: walk the block's children; prefer the **end of a text node** when the offset
  falls on a boundary (typing then continues in the same node); a position before an atom is
  `(block, childIndex)`; an empty block is `(block, 0)`.
* A point cannot be inside an image (`contenteditable=false`); an image is selected and deleted as a unit.
* Round trips are tested for every offset around atoms and marked runs.

## Characters and clusters

Backspace/Delete move by grapheme-cluster boundaries from `Intl.Segmenter` (family emoji with ZWJ,
skin tones, flags, combining accents each delete as one). Intl.Segmenter exists in current Chromium,
Safari 14.1+ and Firefox 125+. **Fallback** when it is missing (`useFallbackSegmentation(true)` in
tests): surrogate pairs, combining marks (`\p{M}`), variation selectors, ZWJ joins and
regional-indicator pairs are handled; dictionary-based and complex scripts (Thai word breaks, Indic
conjuncts) are not.

## Ids

* **Split (Enter)**: the first part keeps the original id; the new block gets a fresh UUID. In a list
  item the item splits (new item id + new paragraph id) and what followed the paragraph moves to it.
* **Merge (Backspace/Delete)**: the earlier block keeps its id and type; the later one disappears.

## What is not handled in the editor core (later checkpoints)

Marks/format commands, lists and tables commands (Tab, empty-item Enter/Backspace), images,
links. Backspace at the start of a list item's first paragraph and any merge across table cells are
deliberate no-ops here. Typing across selected table cells is refused with a visible hint.

## Undo / redo

Snapshots of immutable documents, bounded (100 entries, about 30 MiB of newly allocated blocks;
the most recent step is always kept). Typing in one block with a continuous caret and under one
second between keystrokes is one step; everything else is its own step. Loading a document is not a
step; saving does not clear history; loading another document, or adopting a newer server version
of a clean one, starts a new history (the old snapshots describe a document that no longer
exists). Not durable, not collaborative. Undo cannot produce an invalid document because it only
restores documents that were valid when recorded (tested).

## Verified in

`frontend/test/editor-commands.test.ts` (model, 42), `editor-dom.test.ts` (renderer, mapping,
pipeline, composition, clipboard, history, 41), `editor-surface.test.tsx` (React host, 5), and
`frontend/e2e/editor-core.spec.ts` (real Chromium and Firefox).
