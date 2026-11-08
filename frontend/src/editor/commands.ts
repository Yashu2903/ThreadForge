/**
 * Editing commands: PURE functions from (document, selection, arguments) to a new document and
 * selection. Each returns
 *   { doc, sel }      the change,
 *   { blocked: "..." } the command may not run here (shown to the user as a hint),
 *   null               nothing to do.
 * Nothing here touches the DOM; the editor applies the result, re-renders the changed blocks and
 * records one history entry per command.
 *
 * ID RULES (deterministic, so tests and history can rely on them):
 *   split  - the first part keeps the original block id; the new block gets a fresh UUID;
 *   merge  - the EARLIER block keeps its id and type; the later block disappears.
 */
import type { Heading, Inline, Paragraph } from "threadforge-client/document-schema";
import {
  type Doc,
  type Node,
  type TextBlock,
  blockLength,
  cellOf,
  indexOf,
  isTextBlock,
  newId,
  nodeById,
  parentOf,
  removeNode,
  textBlock,
  updateNode,
  withInlines,
} from "./doc";
import {
  flatten,
  inheritMarks,
  insertAtomInlines,
  insertTextInlines,
  inlineLen2,
  normalizeInlines,
  sliceInlines,
} from "./inline";
import { nextGrapheme, nextWordBoundary, prevGrapheme, prevWordBoundary } from "./segment";

export interface Pos {
  id: string;
  offset: number;
}
export interface Sel {
  anchor: Pos;
  focus: Pos;
}
export type Outcome = { doc: Doc; sel: Sel } | { blocked: string } | null;
export const caret = (id: string, offset: number): Sel => ({ anchor: { id, offset }, focus: { id, offset } });
export const isCollapsed = (s: Sel): boolean => s.anchor.id === s.focus.id && s.anchor.offset === s.focus.offset;

/** The selection's two ends in document order. */
export function ordered(doc: Doc, sel: Sel): { start: Pos; end: Pos } {
  const idx = indexOf(doc);
  const a = idx.orderIdx.get(sel.anchor.id) ?? 0;
  const f = idx.orderIdx.get(sel.focus.id) ?? 0;
  const anchorFirst = a < f || (a === f && sel.anchor.offset <= sel.focus.offset);
  return anchorFirst ? { start: sel.anchor, end: sel.focus } : { start: sel.focus, end: sel.anchor };
}

export const MULTI_CELL_HINT = "This selection spans table cells. Select text inside one cell to type or delete.";

// ---- deleting a range -----------------------------------------------------------------------------------------

export function deleteRange(doc: Doc, start: Pos, end: Pos): { doc: Doc; pos: Pos } | { blocked: string } {
  if (start.id === end.id) {
    if (start.offset === end.offset) return { doc, pos: start };
    const next = updateNode(doc, start.id, (n) => {
      const tb = n as TextBlock;
      return withInlines(
        tb,
        normalizeInlines([...sliceInlines(tb.children, 0, start.offset), ...sliceInlines(tb.children, end.offset, blockLength(tb))]),
      );
    });
    return { doc: next, pos: start };
  }
  const idx = indexOf(doc);
  const iA = idx.orderIdx.get(start.id)!;
  const iB = idx.orderIdx.get(end.id)!;
  const inRange = idx.order.slice(iA, iB + 1);
  const cellA = cellOf(doc, start.id);
  if (cellA !== cellOf(doc, end.id) || (cellA === null && inRange.some((id) => cellOf(doc, id) !== null))) {
    return { blocked: MULTI_CELL_HINT };
  }
  const A = textBlock(doc, start.id);
  const B = textBlock(doc, end.id);
  const merged = normalizeInlines([...sliceInlines(A.children, 0, start.offset), ...sliceInlines(B.children, end.offset, blockLength(B))]);
  let next = updateNode(doc, start.id, (n) => withInlines(n as TextBlock, merged));
  for (const id of inRange.slice(1)) next = removeNode(next, id);
  return { doc: next, pos: start };
}

/** Delete the selection (if any). Returns the collapsed position left behind. */
export function deleteSelection(doc: Doc, sel: Sel): { doc: Doc; pos: Pos } | { blocked: string } {
  const { start, end } = ordered(doc, sel);
  return deleteRange(doc, start, end);
}

// ---- inserting text -------------------------------------------------------------------------------------------

/** Characters the schema forbids in text. Tab is allowed; everything else control-like is dropped. */
export function sanitizeText(text: string): string {
  let out = "";
  for (const ch of text) {
    const o = ch.codePointAt(0)!;
    if (o === 9) out += ch;
    else if (o < 0x20 || (o >= 0x7f && o <= 0x9f) || o === 0x2028 || o === 0x2029) continue;
    else if (o >= 0xd800 && o <= 0xdfff) continue; // a lone surrogate
    else out += ch;
  }
  return out;
}

export function insertText(doc: Doc, sel: Sel, text: string, marksOverride?: import("threadforge-client/document-schema").Marks): Outcome {
  const clean = sanitizeText(text);
  if (!clean) return null;
  let working = doc;
  let pos: Pos = sel.focus;
  if (!isCollapsed(sel)) {
    const r = deleteSelection(doc, sel);
    if ("blocked" in r) return r;
    working = r.doc;
    pos = r.pos;
  }
  const tb = textBlock(working, pos.id);
  const marks = marksOverride ?? inheritMarks(tb.children, pos.offset);
  const next = updateNode(working, pos.id, (n) => withInlines(n as TextBlock, insertTextInlines((n as TextBlock).children, pos.offset, clean, marks)));
  return { doc: next, sel: caret(pos.id, pos.offset + clean.length) };
}

export function insertHardBreak(doc: Doc, sel: Sel): Outcome {
  let working = doc;
  let pos: Pos = sel.focus;
  if (!isCollapsed(sel)) {
    const r = deleteSelection(doc, sel);
    if ("blocked" in r) return r;
    working = r.doc;
    pos = r.pos;
  }
  const next = updateNode(working, pos.id, (n) =>
    withInlines(n as TextBlock, insertAtomInlines((n as TextBlock).children, pos.offset, { type: "hard_break" })),
  );
  return { doc: next, sel: caret(pos.id, pos.offset + 1) };
}

// ---- splitting (Enter) -----------------------------------------------------------------------------------------

export function splitBlock(doc: Doc, sel: Sel): Outcome {
  let working = doc;
  let pos: Pos = sel.focus;
  if (!isCollapsed(sel)) {
    const r = deleteSelection(doc, sel);
    if ("blocked" in r) return r;
    working = r.doc;
    pos = r.pos;
  }
  const tb = textBlock(working, pos.id);
  const length = blockLength(tb);
  const left = sliceInlines(tb.children, 0, pos.offset);
  const right = sliceInlines(tb.children, pos.offset, length);
  const parent = parentOf(working, tb.id);
  const freshId = newId();

  // a heading split at its end continues as a plain paragraph; anywhere else the type is kept
  const tailType: "paragraph" | "heading" = tb.type === "heading" && pos.offset < length ? "heading" : "paragraph";
  const tail: TextBlock =
    tailType === "heading"
      ? withInlines({ type: "heading", id: freshId, level: (tb as Heading).level, ...(tb.attrs ? { attrs: tb.attrs } : {}) } as Heading, right)
      : withInlines({ type: "paragraph", id: freshId, ...(tb.attrs ? { attrs: tb.attrs } : {}) } as Paragraph, right);
  const head = withInlines(tb, left);

  if (parent && parent.type === "list_item" && (parent.children as Node[])[0] === tb) {
    // Enter in a list item: the item splits. Whatever followed the paragraph in the item (a nested
    // list, continuation paragraphs) moves to the new item.
    const rest = (parent.children as Node[]).slice(1);
    const newItem: Node = {
      type: "list_item",
      id: newId(),
      children: [tail, ...rest] as Node[],
    } as Node;
    const next = updateNode(working, parent.id, (item) => [{ ...item, children: [head] } as Node, newItem]);
    return { doc: next, sel: caret(freshId, 0) };
  }
  const next = updateNode(working, tb.id, () => [head, tail]);
  return { doc: next, sel: caret(freshId, 0) };
}

// ---- deleting with Backspace / Delete ---------------------------------------------------------------------------

export type DeleteUnit = "grapheme" | "word" | "line";

export function deleteBackward(doc: Doc, sel: Sel, unit: DeleteUnit = "grapheme"): Outcome {
  if (!isCollapsed(sel)) return fromDelete(deleteSelection(doc, sel));
  const { id, offset } = sel.focus;
  const tb = textBlock(doc, id);
  if (offset === 0) return mergeBackward(doc, tb);
  const text = flatten(tb.children);
  const from = unit === "word" ? prevWordBoundary(text, offset) : unit === "line" ? lineStart(text, offset) : prevGrapheme(text, offset);
  return fromDelete(deleteRange(doc, { id, offset: from }, { id, offset }));
}

export function deleteForward(doc: Doc, sel: Sel, unit: DeleteUnit = "grapheme"): Outcome {
  if (!isCollapsed(sel)) return fromDelete(deleteSelection(doc, sel));
  const { id, offset } = sel.focus;
  const tb = textBlock(doc, id);
  const length = blockLength(tb);
  if (offset >= length) return mergeForward(doc, tb);
  const text = flatten(tb.children);
  const to = unit === "word" ? nextWordBoundary(text, offset) : unit === "line" ? lineEnd(text, offset) : nextGrapheme(text, offset);
  return fromDelete(deleteRange(doc, { id, offset }, { id, offset: to }));
}

function fromDelete(r: { doc: Doc; pos: Pos } | { blocked: string }): Outcome {
  return "blocked" in r ? r : { doc: r.doc, sel: caret(r.pos.id, r.pos.offset) };
}

/** A hard break is a visual line boundary; the start/end of the block are the others. */
const lineStart = (text: string, offset: number): number => text.lastIndexOf("￼", offset - 1) + 1 || 0;
function lineEnd(text: string, offset: number): number {
  const at = text.indexOf("￼", offset);
  return at === -1 ? text.length : at;
}

/**
 * Backspace at the start of a text block: join it onto the previous text block. NOT done (no-op)
 *  - across table cells (a cell boundary is not a merge boundary),
 *  - for the first paragraph of a list item (list behaviour, checkpoint 4, owns that key).
 */
function mergeBackward(doc: Doc, tb: TextBlock): Outcome {
  const idx = indexOf(doc);
  const at = idx.orderIdx.get(tb.id)!;
  if (at === 0) return null;
  const prevId = idx.order[at - 1]!;
  if (cellOf(doc, prevId) !== cellOf(doc, tb.id)) return null;
  const parent = parentOf(doc, tb.id);
  if (parent && parent.type === "list_item" && (parent.children as Node[])[0] === tb) return null;
  const prev = textBlock(doc, prevId);
  const joinAt = blockLength(prev);
  const merged = normalizeInlines([...(prev.children ?? []), ...(tb.children ?? [])]);
  let next = updateNode(doc, prevId, (n) => withInlines(n as TextBlock, merged));
  next = removeNode(next, tb.id);
  return { doc: next, sel: caret(prevId, joinAt) };
}

/** Delete at the end of a text block: pull the next text block's content up (mirror of the above). */
function mergeForward(doc: Doc, tb: TextBlock): Outcome {
  const idx = indexOf(doc);
  const at = idx.orderIdx.get(tb.id)!;
  if (at === idx.order.length - 1) return null;
  const nextId = idx.order[at + 1]!;
  if (cellOf(doc, nextId) !== cellOf(doc, tb.id)) return null;
  const nextBlock = textBlock(doc, nextId);
  const joinAt = blockLength(tb);
  const merged = normalizeInlines([...(tb.children ?? []), ...(nextBlock.children ?? [])]);
  let next = updateNode(doc, tb.id, (n) => withInlines(n as TextBlock, merged));
  next = removeNode(next, nextId);
  return { doc: next, sel: caret(tb.id, joinAt) };
}

// ---- plain text (copy and paste) --------------------------------------------------------------------------------

/** The selection as plain text: blocks joined by \n, cells by \t, rows by \n, hard breaks as \n. */
export function selectionText(doc: Doc, sel: Sel): string {
  const { start, end } = ordered(doc, sel);
  const idx = indexOf(doc);
  const iA = idx.orderIdx.get(start.id)!;
  const iB = idx.orderIdx.get(end.id)!;
  const parts: string[] = [];
  let lastCell: string | null = null;
  let lastRow: string | null = null;
  for (let i = iA; i <= iB; i++) {
    const id = idx.order[i]!;
    const tb = textBlock(doc, id);
    const from = id === start.id ? start.offset : 0;
    const to = id === end.id ? end.offset : blockLength(tb);
    const text = flatten(sliceInlines(tb.children, from, to)).replaceAll("￼", "\n");
    const cell = cellOf(doc, id);
    const row = cell ? idx.parent.get(cell)! : null;
    if (i > iA) parts.push(cell && cell !== lastCell ? (row === lastRow ? "\t" : "\n") : "\n");
    parts.push(text);
    lastCell = cell;
    lastRow = row;
  }
  return parts.join("");
}

/** CRLF and CR become LF; the text is then split into lines. */
export function normalizeNewlines(text: string): string[] {
  return text.replace(/\r\n?/g, "\n").split("\n");
}

/**
 * Plain-text paste. The first line goes in at the caret (replacing the selection); every further
 * line becomes a new block in the CURRENT container via the Enter rules: a paragraph in the body, a
 * list item inside a list, a paragraph inside a cell or quote. Nothing but text is imported.
 */
export function insertPlainText(doc: Doc, sel: Sel, text: string): Outcome {
  const lines = normalizeNewlines(text);
  let current: { doc: Doc; sel: Sel } = { doc, sel };
  let first = true;
  for (const line of lines) {
    if (!first) {
      const split = splitBlock(current.doc, current.sel);
      if (!split || "blocked" in split) return split;
      current = split;
    }
    first = false;
    if (line) {
      const r = insertText(current.doc, current.sel, line);
      if (r && "blocked" in r) return r;
      if (r) current = r;
    } else if (!isCollapsed(current.sel)) {
      const r = deleteSelection(current.doc, current.sel);
      if ("blocked" in r) return r;
      current = { doc: r.doc, sel: caret(r.pos.id, r.pos.offset) };
    }
  }
  if (current.doc === doc) return null;
  return current;
}

export type { Inline };
export { inlineLen2, isTextBlock, nodeById };
