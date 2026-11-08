/**
 * Structure commands (pure): lists, quotes and tables, plus the list-aware Enter and Backspace.
 *
 * Document rules these enforce (docs/document-schema.md): lists nest at most 3 deep and only occur
 * at the top level or inside list items; a list item starts with a paragraph; quotes and table cells
 * hold paragraphs only (no headings, lists, quotes or tables inside); tables are rectangular, at
 * most 50 rows x 20 columns, and are never nested.
 */
import type { List, ListItem, Paragraph, Table, TableCell, TableRow } from "threadforge-client/document-schema";
import {
  type Outcome,
  type Sel,
  caret,
  deleteBackward,
  isCollapsed,
  ordered,
  splitBlock,
  type DeleteUnit,
} from "./commands";
import { type Doc, type Node, type TextBlock, blockLength, indexOf, newId, nodeById, parentOf, removeNode, textBlock, updateNode } from "./doc";
import { blockRanges } from "./format";

const MAX_LIST_DEPTH = 3;
const MAX_ROWS = 50;
const MAX_COLS = 20;

// ---- helpers ------------------------------------------------------------------------------------------------------

interface ListContext {
  item: ListItem;
  list: List;
  depth: number; // 1 = a list directly in the body
  first: boolean; // the text block is the item's first child
}

export function listContext(doc: Doc, id: string): ListContext | null {
  const idx = indexOf(doc);
  const parentId = idx.parent.get(id);
  const item = parentId ? idx.nodes.get(parentId) : undefined;
  if (!item || item.type !== "list_item") return null;
  const list = idx.nodes.get(idx.parent.get(item.id)!) as List;
  const depth = idx.chain(id).filter((c) => idx.nodes.get(c)!.type === "list").length;
  return { item, list, depth, first: item.children[0]!.id === id };
}

const asParagraph = (tb: TextBlock): Paragraph => {
  if (tb.type === "paragraph") return tb;
  const { level: _level, ...rest } = tb;
  return { ...rest, type: "paragraph" } as Paragraph;
};

function stripIndent(p: Paragraph): Paragraph {
  if (!p.attrs?.indent_level) return p;
  const { indent_level: _drop, ...attrs } = p.attrs;
  const { attrs: _old, ...rest } = p;
  return Object.keys(attrs).length ? { ...rest, attrs } : rest;
}

function nestedDepth(node: Node): number {
  if (node.type === "list") return 1 + Math.max(0, ...node.children.map(nestedDepth));
  if (node.type === "list_item") return Math.max(0, ...(node.children as Node[]).map(nestedDepth));
  return 0;
}

// ---- lists ---------------------------------------------------------------------------------------------------------

/** Replace a body-level list by: items before it, the item's content as plain blocks, items after it. */
function liftItem(doc: Doc, itemId: string): Doc {
  const idx = indexOf(doc);
  const item = idx.nodes.get(itemId) as ListItem;
  const list = idx.nodes.get(idx.parent.get(itemId)!) as List;
  const i = list.children.findIndex((c) => c.id === itemId);
  const before = list.children.slice(0, i);
  const after = list.children.slice(i + 1);
  const content = item.children as Node[];
  const out: Node[] = [];
  if (before.length) out.push({ ...list, children: before });
  out.push(...content);
  if (after.length) out.push({ ...list, id: before.length ? newId() : list.id, children: after });
  return updateNode(doc, list.id, () => out);
}

/** Move a nested item up one level; the siblings after it become its children (the usual outline rule). */
function outdentNested(doc: Doc, itemId: string): Doc {
  const idx = indexOf(doc);
  const item = idx.nodes.get(itemId) as ListItem;
  const inner = idx.nodes.get(idx.parent.get(itemId)!) as List;
  const parentItem = idx.nodes.get(idx.parent.get(inner.id)!) as ListItem;
  const outer = idx.nodes.get(idx.parent.get(parentItem.id)!) as List;
  const i = inner.children.findIndex((c) => c.id === itemId);
  const tail = inner.children.slice(i + 1);
  let moved: ListItem = item;
  if (tail.length) {
    const last = item.children[item.children.length - 1] as Node;
    moved =
      last.type === "list"
        ? { ...item, children: [...item.children.slice(0, -1), { ...last, children: [...last.children, ...tail] }] as ListItem["children"] }
        : { ...item, children: [...item.children, { type: "list", id: newId(), ...(inner.ordered ? { ordered: true as const } : {}), children: tail } as List] as ListItem["children"] };
  }
  const keep = inner.children.slice(0, i);
  const updatedParent: ListItem = {
    ...parentItem,
    children: (keep.length
      ? parentItem.children.map((c) => (c.id === inner.id ? { ...inner, children: keep } : c))
      : parentItem.children.filter((c) => c.id !== inner.id)) as ListItem["children"],
  };
  return updateNode(doc, outer.id, () => {
    const at = outer.children.findIndex((c) => c.id === parentItem.id);
    return { ...outer, children: [...outer.children.slice(0, at), updatedParent, moved, ...outer.children.slice(at + 1)] } as Node;
  });
}

function outdentOrLift(doc: Doc, itemId: string): Doc {
  const ctx = listContext(doc, (nodeById(doc, itemId) as ListItem).children[0]!.id)!;
  return ctx.depth > 1 ? outdentNested(doc, itemId) : liftItem(doc, itemId);
}

function selectedItems(doc: Doc, sel: Sel): string[] {
  const seen: string[] = [];
  for (const r of blockRanges(doc, sel)) {
    const ctx = listContext(doc, r.id);
    if (ctx && !seen.includes(ctx.item.id)) seen.push(ctx.item.id);
  }
  return seen;
}

export function toggleList(doc: Doc, sel: Sel, wantOrdered: boolean): Outcome {
  const ids = blockRanges(doc, sel).map((r) => r.id);
  const contexts = ids.map((id) => listContext(doc, id));
  if (contexts.every(Boolean)) {
    const lists = new Map(contexts.map((c) => [c!.list.id, c!.list]));
    if ([...lists.values()].every((l) => !!l.ordered === wantOrdered)) {
      // already this kind of list: toggle it off
      let next = doc;
      for (const itemId of selectedItems(doc, sel).reverse()) next = outdentOrLift(next, itemId);
      return next === doc ? null : { doc: next, sel };
    }
    let next = doc;
    for (const list of lists.values()) {
      next = updateNode(next, list.id, (n) => {
        const { ordered: _o, ...rest } = n as List;
        return (wantOrdered ? { ...rest, ordered: true } : rest) as List;
      });
    }
    return { doc: next, sel };
  }
  const eligible = new Set(ids.filter((id, i) => !contexts[i] && parentOf(doc, id) === null));
  if (eligible.size === 0) return { blocked: "Lists can only be made from paragraphs in the body, not inside tables or quotes." };
  const blocks: Node[] = [];
  let run: ListItem[] = [];
  const flush = () => {
    if (run.length) blocks.push({ type: "list", id: newId(), ...(wantOrdered ? { ordered: true as const } : {}), children: run } as List);
    run = [];
  };
  for (const b of doc.blocks) {
    if (eligible.has(b.id)) {
      run.push({ type: "list_item", id: newId(), children: [stripIndent(asParagraph(b as TextBlock))] });
    } else {
      flush();
      blocks.push(b);
    }
  }
  flush();
  return { doc: { ...doc, blocks: blocks as Doc["blocks"] }, sel };
}

export function indentListItems(doc: Doc, sel: Sel): Outcome {
  const items = selectedItems(doc, sel);
  if (items.length === 0) return null;
  let next = doc;
  let blocked: string | null = null;
  for (const itemId of items) {
    const idx = indexOf(next);
    const item = idx.nodes.get(itemId) as ListItem;
    const list = idx.nodes.get(idx.parent.get(itemId)!) as List;
    const i = list.children.findIndex((c) => c.id === itemId);
    if (i === 0) {
      blocked = "The first item in a list can't be indented. Indent the items after it.";
      continue;
    }
    const depth = idx.chain(itemId).filter((c) => idx.nodes.get(c)!.type === "list").length;
    if (depth + 1 + nestedDepth(item) > MAX_LIST_DEPTH) {
      blocked = `Lists can be nested at most ${MAX_LIST_DEPTH} levels deep.`;
      continue;
    }
    const prev = list.children[i - 1]!;
    const last = prev.children[prev.children.length - 1] as Node;
    const grown: ListItem =
      last.type === "list"
        ? { ...prev, children: [...prev.children.slice(0, -1), { ...last, children: [...last.children, item] }] as ListItem["children"] }
        : { ...prev, children: [...prev.children, { type: "list", id: newId(), ...(list.ordered ? { ordered: true as const } : {}), children: [item] } as List] as ListItem["children"] };
    next = updateNode(next, list.id, () => ({ ...list, children: [...list.children.slice(0, i - 1), grown, ...list.children.slice(i + 1)] }) as Node);
  }
  if (next === doc) return blocked ? { blocked } : null;
  return { doc: next, sel };
}

export function outdentListItems(doc: Doc, sel: Sel): Outcome {
  const items = selectedItems(doc, sel);
  if (items.length === 0) return null;
  let next = doc;
  for (const itemId of [...items].reverse()) next = outdentOrLift(next, itemId);
  return next === doc ? null : { doc: next, sel };
}

/** Enter: in an EMPTY list item it leaves the list (or goes up a level); otherwise it splits. */
export function enter(doc: Doc, sel: Sel): Outcome {
  if (isCollapsed(sel)) {
    const ctx = listContext(doc, sel.focus.id);
    if (ctx?.first && blockLength(textBlock(doc, sel.focus.id)) === 0 && ctx.item.children.length === 1) {
      return { doc: outdentOrLift(doc, ctx.item.id), sel };
    }
  }
  return splitBlock(doc, sel);
}

/** Backspace: at the start of a list item it takes the bullet off (or goes up a level); otherwise as usual. */
export function backspace(doc: Doc, sel: Sel, unit: DeleteUnit = "grapheme"): Outcome {
  if (isCollapsed(sel) && sel.focus.offset === 0) {
    const ctx = listContext(doc, sel.focus.id);
    if (ctx?.first) return { doc: outdentOrLift(doc, ctx.item.id), sel };
  }
  return deleteBackward(doc, sel, unit);
}

export function listState(doc: Doc, sel: Sel): { ordered: boolean | "mixed"; depth: number } | null {
  const ctxs = blockRanges(doc, sel).map((r) => listContext(doc, r.id));
  if (ctxs.length === 0 || !ctxs.every(Boolean)) return null;
  const kinds = new Set(ctxs.map((c) => !!c!.list.ordered));
  return { ordered: kinds.size > 1 ? "mixed" : [...kinds][0]!, depth: Math.max(...ctxs.map((c) => c!.depth)) };
}

// ---- quotes ------------------------------------------------------------------------------------------------------------

export function toggleQuote(doc: Doc, sel: Sel): Outcome {
  const ids = blockRanges(doc, sel).map((r) => r.id);
  const quotes = new Map<string, Node>();
  for (const id of ids) {
    const p = parentOf(doc, id);
    if (p?.type === "quote") quotes.set(p.id, p);
  }
  if (ids.every((id) => parentOf(doc, id)?.type === "quote")) {
    let next = doc;
    for (const q of quotes.values()) next = updateNode(next, q.id, (n) => (n as { children: Node[] }).children);
    return { doc: next, sel };
  }
  const eligible = new Set(ids.filter((id) => parentOf(doc, id) === null));
  if (eligible.size === 0) return { blocked: "Quotes can only be made from paragraphs in the body." };
  const blocks: Node[] = [];
  let run: Paragraph[] = [];
  const flush = () => {
    if (run.length) blocks.push({ type: "quote", id: newId(), children: run } as Node);
    run = [];
  };
  for (const b of doc.blocks) {
    if (eligible.has(b.id)) run.push(asParagraph(b as TextBlock));
    else {
      flush();
      blocks.push(b);
    }
  }
  flush();
  return { doc: { ...doc, blocks: blocks as Doc["blocks"] }, sel };
}

export const inQuote = (doc: Doc, sel: Sel): boolean => blockRanges(doc, sel).every((r) => parentOf(doc, r.id)?.type === "quote");

// ---- tables ----------------------------------------------------------------------------------------------------------------

export interface TableContext {
  table: Table;
  row: TableRow;
  cell: TableCell;
  rowIndex: number;
  colIndex: number;
  rows: number;
  cols: number;
}

export function tableContext(doc: Doc, id: string): TableContext | null {
  const idx = indexOf(doc);
  const chain = idx.chain(id).map((c) => idx.nodes.get(c)!);
  const cell = chain.find((n) => n.type === "table_cell") as TableCell | undefined;
  if (!cell) return null;
  const row = idx.nodes.get(idx.parent.get(cell.id)!) as TableRow;
  const table = idx.nodes.get(idx.parent.get(row.id)!) as Table;
  return {
    table,
    row,
    cell,
    rowIndex: table.children.findIndex((r) => r.id === row.id),
    colIndex: row.children.findIndex((c) => c.id === cell.id),
    rows: table.children.length,
    cols: row.children.length,
  };
}

const newCell = (header = false): TableCell => ({
  type: "table_cell",
  id: newId(),
  ...(header ? { header: true as const } : {}),
  children: [{ type: "paragraph", id: newId() }],
});
const newRow = (cols: number, header = false): TableRow => ({
  type: "table_row",
  id: newId(),
  children: Array.from({ length: cols }, () => newCell(header)),
});
const firstBlockOf = (cell: TableCell): string => cell.children[0]!.id;

export function insertTable(doc: Doc, sel: Sel, rows: number, cols: number, headerRow: boolean): Outcome {
  if (!(Number.isInteger(rows) && rows >= 1 && rows <= MAX_ROWS && Number.isInteger(cols) && cols >= 1 && cols <= MAX_COLS)) {
    return { blocked: `A table can have 1 to ${MAX_ROWS} rows and 1 to ${MAX_COLS} columns.` };
  }
  const { end } = ordered(doc, sel);
  if (tableContext(doc, end.id)) return { blocked: "A table can't be placed inside another table." };
  const top = indexOf(doc).chain(end.id)[0]!;
  const table: Table = {
    type: "table",
    id: newId(),
    children: Array.from({ length: rows }, (_, r) => newRow(cols, headerRow && r === 0)),
  };
  const isLast = doc.blocks[doc.blocks.length - 1]!.id === top;
  const next = updateNode(doc, top, (n) => [n, table as Node, ...(isLast ? [{ type: "paragraph", id: newId() } as Node] : [])]);
  return { doc: next, sel: caret(firstBlockOf(table.children[0]!.children[0]!), 0) };
}

function withTable(doc: Doc, sel: Sel): TableContext | { blocked: string } {
  const ctx = tableContext(doc, sel.focus.id);
  return ctx ?? { blocked: "Put the cursor inside a table first." };
}

export function addRow(doc: Doc, sel: Sel, where: "above" | "below"): Outcome {
  const ctx = withTable(doc, sel);
  if ("blocked" in ctx) return ctx;
  if (ctx.rows >= MAX_ROWS) return { blocked: `A table can have at most ${MAX_ROWS} rows.` };
  const fresh = newRow(ctx.cols);
  const at = where === "above" ? ctx.rowIndex : ctx.rowIndex + 1;
  const rows = [...ctx.table.children.slice(0, at), fresh, ...ctx.table.children.slice(at)];
  return { doc: updateNode(doc, ctx.table.id, () => ({ ...ctx.table, children: rows }) as Node), sel: caret(firstBlockOf(fresh.children[Math.min(ctx.colIndex, ctx.cols - 1)]!), 0) };
}

export function addColumn(doc: Doc, sel: Sel, where: "left" | "right"): Outcome {
  const ctx = withTable(doc, sel);
  if ("blocked" in ctx) return ctx;
  if (ctx.cols >= MAX_COLS) return { blocked: `A table can have at most ${MAX_COLS} columns.` };
  const at = where === "left" ? ctx.colIndex : ctx.colIndex + 1;
  let focusCell: TableCell | null = null;
  const rows = ctx.table.children.map((r, ri) => {
    const reference = r.children[ctx.colIndex]!;
    const cell = newCell(!!reference.header);
    if (ri === ctx.rowIndex) focusCell = cell;
    return { ...r, children: [...r.children.slice(0, at), cell, ...r.children.slice(at)] };
  });
  return { doc: updateNode(doc, ctx.table.id, () => ({ ...ctx.table, children: rows }) as Node), sel: caret(firstBlockOf(focusCell!), 0) };
}

export function removeRow(doc: Doc, sel: Sel): Outcome {
  const ctx = withTable(doc, sel);
  if ("blocked" in ctx) return ctx;
  if (ctx.rows === 1) return { blocked: "A table must keep at least one row. Use Delete table to remove it." };
  const keep = Math.min(ctx.rowIndex, ctx.rows - 2);
  const next = removeNode(doc, ctx.row.id);
  const target = ctx.table.children.filter((r) => r.id !== ctx.row.id)[keep]!;
  return { doc: next, sel: caret(firstBlockOf(target.children[Math.min(ctx.colIndex, ctx.cols - 1)]!), 0) };
}

export function removeColumn(doc: Doc, sel: Sel): Outcome {
  const ctx = withTable(doc, sel);
  if ("blocked" in ctx) return ctx;
  if (ctx.cols === 1) return { blocked: "A table must keep at least one column. Use Delete table to remove it." };
  const rows = ctx.table.children.map((r) => ({ ...r, children: r.children.filter((_, i) => i !== ctx.colIndex) }));
  const keep = Math.min(ctx.colIndex, ctx.cols - 2);
  return { doc: updateNode(doc, ctx.table.id, () => ({ ...ctx.table, children: rows }) as Node), sel: caret(firstBlockOf(rows[ctx.rowIndex]!.children[keep]!), 0) };
}

export function deleteTable(doc: Doc, sel: Sel): Outcome {
  const ctx = withTable(doc, sel);
  if ("blocked" in ctx) return ctx;
  const idx = indexOf(doc);
  const inTable = idx.order.filter((id) => idx.chain(id).includes(ctx.table.id));
  const firstAt = idx.orderIdx.get(inTable[0]!)!;
  const before = firstAt > 0 ? idx.order[firstAt - 1]! : null; // the text block just above the table
  const next = removeNode(doc, ctx.table.id);
  const order = indexOf(next).order;
  if (before && order.includes(before)) return { doc: next, sel: caret(before, blockLength(textBlock(next, before))) };
  return { doc: next, sel: caret(order[0]!, 0) };
}

/** Tab / Shift+Tab inside a table: next/previous cell; Tab in the last cell appends a row. */
export function tableTab(doc: Doc, sel: Sel, backward: boolean): Outcome {
  const ctx = tableContext(doc, sel.focus.id);
  if (!ctx) return null;
  const linear = ctx.rowIndex * ctx.cols + ctx.colIndex + (backward ? -1 : 1);
  if (linear < 0) return { doc, sel }; // Shift+Tab in the first cell: nothing happens (and focus stays)
  if (linear >= ctx.rows * ctx.cols) {
    if (ctx.rows >= MAX_ROWS) return { blocked: `A table can have at most ${MAX_ROWS} rows.` };
    const fresh = newRow(ctx.cols);
    const next = updateNode(doc, ctx.table.id, () => ({ ...ctx.table, children: [...ctx.table.children, fresh] }) as Node);
    return { doc: next, sel: caret(firstBlockOf(fresh.children[0]!), 0) };
  }
  const cell = ctx.table.children[Math.floor(linear / ctx.cols)]!.children[linear % ctx.cols]!;
  return { doc, sel: caret(firstBlockOf(cell), 0) };
}
