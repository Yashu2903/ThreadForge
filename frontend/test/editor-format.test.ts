import { describe, expect, it } from "vitest";
import { validateDocument } from "threadforge-client/document-schema";
import { type Outcome, type Sel, caret } from "../src/editor/commands";
import type { Doc } from "../src/editor/doc";
import { textBlock } from "../src/editor/doc";
import {
  attrState,
  blockKindState,
  boolMarkState,
  changeIndent,
  linkAt,
  removeLink,
  setAttrs,
  setBlockKind,
  setLink,
  setMark,
  toggleMark,
  valueMarkState,
} from "../src/editor/format";
import {
  addColumn,
  addRow,
  backspace,
  deleteTable,
  enter,
  indentListItems,
  insertTable,
  listState,
  outdentListItems,
  removeColumn,
  removeRow,
  tableContext,
  tableTab,
  toggleList,
  toggleQuote,
} from "../src/editor/structure";
import { flatten } from "../src/editor/inline";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const t = (text: string, marks?: object) => ({ type: "text" as const, text, ...(marks ? { marks } : {}) });
const p = (n: number, ...children: object[]) => ({ type: "paragraph" as const, id: id(n), ...(children.length ? { children } : {}) });
const doc = (...blocks: object[]): Doc => ({ schema_version: 1, blocks: blocks as never });
const li = (n: number, ...children: object[]) => ({ type: "list_item", id: id(n), children });
const ul = (n: number, ordered: boolean, ...items: object[]) => ({ type: "list", id: id(n), ...(ordered ? { ordered: true } : {}), children: items });
const cell = (n: number, ...ps: object[]) => ({ type: "table_cell", id: id(n), children: ps });
const row = (n: number, ...cells: object[]) => ({ type: "table_row", id: id(n), children: cells });
const table = (n: number, ...rows: object[]) => ({ type: "table", id: id(n), children: rows });
const range = (a: number, ao: number, b: number, bo: number): Sel => ({ anchor: { id: id(a), offset: ao }, focus: { id: id(b), offset: bo } });

const ok = (r: Outcome): { doc: Doc; sel: Sel } => {
  expect(r, JSON.stringify(r)).not.toBeNull();
  expect(r).not.toHaveProperty("blocked");
  const out = r as { doc: Doc; sel: Sel };
  expect(() => validateDocument(out.doc), "result must stay valid").not.toThrow();
  return out;
};
const kids = (d: Doc, n: number) => textBlock(d, id(n)).children;

describe("marks", () => {
  it("toggles bold over part of a run, splitting it, and back again", () => {
    const d = doc(p(1, t("hello world")));
    const on = ok(toggleMark(d, range(1, 6, 1, 11), "bold"));
    expect(kids(on.doc, 1)).toEqual([t("hello "), t("world", { bold: true })]);
    const off = ok(toggleMark(on.doc, range(1, 6, 1, 11), "bold"));
    expect(kids(off.doc, 1)).toEqual([t("hello world")]); // merged back, no marks key left behind
  });

  it("a mixed selection turns the mark ON everywhere; tri-state reports on / off / mixed", () => {
    const d = doc(p(1, t("ab", { bold: true }), t("cd")));
    expect(boolMarkState(d, range(1, 0, 1, 2), "bold")).toBe("on");
    expect(boolMarkState(d, range(1, 2, 1, 4), "bold")).toBe("off");
    expect(boolMarkState(d, range(1, 0, 1, 4), "bold")).toBe("mixed");
    const all = ok(toggleMark(d, range(1, 0, 1, 4), "bold"));
    expect(kids(all.doc, 1)).toEqual([t("abcd", { bold: true })]);
  });

  it("works across blocks and across table cells (allowed for marks)", () => {
    const d = doc(table(10, row(11, cell(12, p(1, t("ab"))), cell(13, p(2, t("cd"))))));
    const r = ok(toggleMark(d, range(1, 1, 2, 1), "italic"));
    expect(kids(r.doc, 1)).toEqual([t("a"), t("b", { italic: true })]);
    expect(kids(r.doc, 2)).toEqual([t("c", { italic: true }), t("d")]);
  });

  it("an image in the range is skipped; a collapsed caret changes nothing in the document", () => {
    const d = doc(p(1, t("a"), { type: "image", id: id(9), asset_id: id(8), width_px: 5, height_px: 5 }, t("b")));
    expect(kids(ok(toggleMark(d, range(1, 0, 1, 3), "bold")).doc, 1)).toEqual([
      t("a", { bold: true }),
      { type: "image", id: id(9), asset_id: id(8), width_px: 5, height_px: 5 },
      t("b", { bold: true }),
    ]);
    expect(toggleMark(d, caret(id(1), 1), "bold")).toBeNull();
  });

  it("sets font, size, colour, highlight and script, and clears them with null", () => {
    let d = doc(p(1, t("abcd")));
    d = ok(setMark(d, range(1, 0, 1, 2), "font", "serif")).doc;
    d = ok(setMark(d, range(1, 0, 1, 2), "size_pt", 14.5)).doc;
    d = ok(setMark(d, range(1, 0, 1, 2), "color", "#ff0000")).doc;
    d = ok(setMark(d, range(1, 2, 1, 4), "script", "superscript")).doc;
    expect(kids(d, 1)).toEqual([t("ab", { font: "serif", size_pt: 14.5, color: "#FF0000" }), t("cd", { script: "superscript" })]);
    expect(valueMarkState(d, range(1, 0, 1, 2), "font")).toBe("serif");
    expect(valueMarkState(d, range(1, 0, 1, 4), "font")).toBe("mixed");
    expect(valueMarkState(d, range(1, 2, 1, 4), "font")).toBeNull();
    d = ok(setMark(d, range(1, 0, 1, 4), "font", null)).doc;
    expect(valueMarkState(d, range(1, 0, 1, 4), "font")).toBeNull();
  });

  it("script is one field: superscript replaces subscript", () => {
    const d = ok(setMark(doc(p(1, t("x", { script: "subscript" }))), range(1, 0, 1, 1), "script", "superscript")).doc;
    expect(kids(d, 1)).toEqual([t("x", { script: "superscript" })]);
  });

  it("refuses values the schema would refuse", () => {
    const d = doc(p(1, t("abc")));
    for (const [key, value] of [["size_pt", 7], ["size_pt", 10.25], ["color", "red"], ["font", "Comic Sans"], ["script", "up"]] as const) {
      expect(setMark(d, range(1, 0, 1, 3), key, value as never), `${key}=${value}`).toHaveProperty("blocked");
    }
  });
});

describe("links", () => {
  it("links a selection, finds it from a caret inside, and edits or removes the whole link", () => {
    const d = doc(p(1, t("see the docs now")));
    const linked = ok(setLink(d, range(1, 8, 1, 12), "https://example.com/docs"));
    expect(kids(linked.doc, 1)).toEqual([t("see the "), t("docs", { link: { href: "https://example.com/docs" } }), t(" now")]);
    expect(linkAt(linked.doc, caret(id(1), 10))).toEqual({ href: "https://example.com/docs", id: id(1), from: 8, to: 12 });
    expect(linkAt(linked.doc, caret(id(1), 8))?.href).toBe("https://example.com/docs"); // at the edge, next char is linked
    expect(linkAt(linked.doc, caret(id(1), 2))).toBeNull();
    const edited = ok(setLink(linked.doc, caret(id(1), 10), "mailto:me@example.com"));
    expect(kids(edited.doc, 1)![1]).toEqual(t("docs", { link: { href: "mailto:me@example.com" } }));
    const removed = ok(removeLink(edited.doc, caret(id(1), 10)));
    expect(kids(removed.doc, 1)).toEqual([t("see the docs now")]);
  });

  it("a link split by other marks is still one link", () => {
    const d = doc(p(1, t("ab", { link: { href: "https://e.com" } }), t("cd", { link: { href: "https://e.com" }, bold: true })));
    expect(linkAt(d, caret(id(1), 1))).toMatchObject({ from: 0, to: 4 });
  });

  it("rejects unsafe or malformed links with a hint and changes nothing", () => {
    const d = doc(p(1, t("click")));
    for (const href of ["javascript:alert(1)", "data:text/html,x", "example.com", "//e.com", "java\nscript:x", "https://u:p@e.com", ""]) {
      expect(setLink(d, range(1, 0, 1, 5), href), href).toHaveProperty("blocked");
    }
    expect(setLink(d, caret(id(1), 2), "https://e.com")).toHaveProperty("blocked"); // nothing selected
  });

  it("normalizes the link (trim, lowercase scheme)", () => {
    const r = ok(setLink(doc(p(1, t("x"))), range(1, 0, 1, 1), "  HTTPS://Example.com/A  "));
    expect(kids(r.doc, 1)).toEqual([t("x", { link: { href: "https://Example.com/A" } })]);
  });
});

describe("paragraph attributes, indent and block kind", () => {
  it("sets alignment, spacing and line spacing on every selected block and drops defaults", () => {
    const d = doc(p(1, t("a")), p(2, t("b")));
    const r = ok(setAttrs(d, range(1, 0, 2, 1), { align: "center", line_spacing: 1.5, space_after_pt: 12 }));
    for (const n of [1, 2]) expect(textBlock(r.doc, id(n)).attrs).toEqual({ align: "center", line_spacing: 1.5, space_after_pt: 12 });
    const back = ok(setAttrs(r.doc, range(1, 0, 2, 1), { align: "left" as never, line_spacing: 1, space_after_pt: 0 }));
    expect(textBlock(back.doc, id(1))).not.toHaveProperty("attrs");
    expect(attrState(r.doc, range(1, 0, 2, 1), "align")).toBe("center");
  });

  it("reports mixed values and rejects invalid ones", () => {
    const d = doc({ ...p(1, t("a")), attrs: { align: "right" } }, p(2, t("b")));
    expect(attrState(d, range(1, 0, 2, 1), "align")).toBe("mixed");
    for (const patch of [{ align: "start" }, { line_spacing: 1.3 }, { space_before_pt: 73 }, { space_after_pt: 1.2 }, { indent_level: 9 }]) {
      expect(setAttrs(d, range(1, 0, 1, 1), patch as never), JSON.stringify(patch)).toHaveProperty("blocked");
    }
  });

  it("indent moves by whole levels, is clamped to 0..8, and is ignored in list items", () => {
    const d = doc(p(1, t("a")), ul(10, false, li(11, p(2, t("item")))));
    let r = ok(changeIndent(d, range(1, 0, 1, 1), 1));
    expect(textBlock(r.doc, id(1)).attrs).toEqual({ indent_level: 1 });
    for (let i = 0; i < 12; i++) r = ok(changeIndent(r.doc, range(1, 0, 1, 1), 1) ?? { doc: r.doc, sel: r.sel });
    expect(textBlock(r.doc, id(1)).attrs).toEqual({ indent_level: 8 });
    expect(changeIndent(d, range(2, 0, 2, 1), 1)).toBeNull();
    expect(textBlock(ok(changeIndent(r.doc, range(1, 0, 1, 1), -20)).doc, id(1))).not.toHaveProperty("attrs");
  });

  it("converts paragraphs and headings, keeping ids and attributes; headings only in the body", () => {
    const d = doc({ ...p(1, t("Title")), attrs: { align: "center" } }, ul(10, false, li(11, p(2, t("item")))));
    const h = ok(setBlockKind(d, range(1, 0, 1, 5), "h2"));
    expect(h.doc.blocks[0]).toMatchObject({ type: "heading", level: 2, id: id(1), attrs: { align: "center" } });
    expect(blockKindState(h.doc, range(1, 0, 1, 5))).toBe("h2");
    const back = ok(setBlockKind(h.doc, range(1, 0, 1, 5), "paragraph"));
    expect(back.doc.blocks[0]).toMatchObject({ type: "paragraph", id: id(1) });
    expect(back.doc.blocks[0]).not.toHaveProperty("level");
    expect(setBlockKind(d, range(2, 0, 2, 2), "h1")).toHaveProperty("blocked"); // not inside a list
  });
});

describe("lists", () => {
  it("makes consecutive body paragraphs one list; headings become paragraphs", () => {
    const d = doc(p(1, t("a")), { type: "heading", id: id(2), level: 1, children: [t("b")] }, p(3, t("c")), p(4, t("d")));
    const r = ok(toggleList(d, range(1, 0, 3, 1), false));
    expect(r.doc.blocks.map((b) => b.type)).toEqual(["list", "paragraph"]);
    const list = r.doc.blocks[0] as never as { children: { children: { id: string; type: string }[] }[] };
    expect(list.children.map((i) => i.children[0]!.id)).toEqual([id(1), id(2), id(3)]); // block ids survive
    expect(list.children[1]!.children[0]!.type).toBe("paragraph");
    expect(listState(r.doc, range(1, 0, 3, 1))).toEqual({ ordered: false, depth: 1 });
  });

  it("switches bullet <-> numbered, and toggling the same kind removes the list", () => {
    const d = doc(ul(10, false, li(11, p(1, t("a"))), li(12, p(2, t("b")))));
    const numbered = ok(toggleList(d, range(1, 0, 2, 1), true));
    expect((numbered.doc.blocks[0] as never as { ordered: boolean }).ordered).toBe(true);
    const gone = ok(toggleList(numbered.doc, range(1, 0, 2, 1), true));
    expect(gone.doc.blocks.map((b) => b.type)).toEqual(["paragraph", "paragraph"]);
  });

  it("removing one item from the middle splits the list around it", () => {
    const d = doc(ul(10, false, li(11, p(1, t("a"))), li(12, p(2, t("b"))), li(13, p(3, t("c")))));
    const r = ok(toggleList(d, range(2, 0, 2, 1), false));
    expect(r.doc.blocks.map((b) => b.type)).toEqual(["list", "paragraph", "list"]);
    expect(new Set(r.doc.blocks.map((b) => b.id)).size).toBe(3);
  });

  it("Tab indents under the previous item; the first item cannot be indented; depth is capped at 3", () => {
    const d = doc(ul(10, false, li(11, p(1, t("a"))), li(12, p(2, t("b")))));
    expect(indentListItems(d, caret(id(1), 0))).toHaveProperty("blocked");
    const r = ok(indentListItems(d, caret(id(2), 0)));
    const first = (r.doc.blocks[0] as never as { children: { children: { type: string; children?: object[] }[] }[] }).children;
    expect(first).toHaveLength(1);
    expect(first[0]!.children.map((c) => c.type)).toEqual(["paragraph", "list"]);
    expect(listState(r.doc, caret(id(2), 0))).toEqual({ ordered: false, depth: 2 });
    // a third level is fine, a fourth is refused
    let deep = doc(ul(10, false, li(11, p(1, t("a"))), li(12, p(2, t("b"))), li(13, p(3, t("c"))), li(14, p(4, t("d")))));
    deep = ok(indentListItems(deep, caret(id(2), 0))).doc; // b under a
    deep = ok(indentListItems(deep, caret(id(3), 0))).doc; // c under a (after b)
    deep = ok(indentListItems(deep, caret(id(3), 0))).doc; // c under b: depth 3
    expect(listState(deep, caret(id(3), 0))?.depth).toBe(3);
    deep = ok(indentListItems(deep, caret(id(4), 0))).doc; // d under a
    deep = ok(indentListItems(deep, caret(id(4), 0))).doc; // d under b: depth 3
    expect(listState(deep, caret(id(4), 0))?.depth).toBe(3);
    expect(indentListItems(deep, caret(id(4), 0))).toHaveProperty("blocked"); // under c would be depth 4
  });

  it("Shift+Tab outdents; the siblings after the item become its children; at depth 1 it leaves the list", () => {
    let d = doc(ul(10, true, li(11, p(1, t("a"))), li(12, p(2, t("b"))), li(13, p(3, t("c")))));
    d = ok(indentListItems(d, caret(id(2), 0))).doc;
    d = ok(indentListItems(d, caret(id(3), 0))).doc; // a > [b, c]
    const out = ok(outdentListItems(d, caret(id(2), 0)));
    const top = (out.doc.blocks[0] as never as { children: { children: { type: string }[] }[] }).children;
    expect(top).toHaveLength(2); // a, b
    expect(top[1]!.children.map((c) => c.type)).toEqual(["paragraph", "list"]); // c moved under b
    expect(listState(out.doc, caret(id(3), 0))?.depth).toBe(2);
    const left = ok(outdentListItems(out.doc, caret(id(2), 0)));
    expect(left.doc.blocks.map((b) => b.type)).toEqual(["list", "paragraph", "list"]);
  });

  it("Enter in an empty item leaves the list; Backspace at the start of an item takes the bullet off", () => {
    const d = doc(ul(10, false, li(11, p(1, t("a"))), li(12, p(2))));
    const left = ok(enter(d, caret(id(2), 0)));
    expect(left.doc.blocks.map((b) => b.type)).toEqual(["list", "paragraph"]);
    const withText = doc(ul(10, false, li(11, p(1, t("a"))), li(12, p(2, t("bc")))));
    const split = ok(enter(withText, caret(id(2), 1)));
    expect((split.doc.blocks[0] as never as { children: object[] }).children).toHaveLength(3); // a normal split
    const unbulleted = ok(backspace(withText, caret(id(2), 0)));
    expect(unbulleted.doc.blocks.map((b) => b.type)).toEqual(["list", "paragraph"]);
    expect(flatten(textBlock(unbulleted.doc, id(2)).children)).toBe("bc");
    // not at the start: ordinary Backspace
    expect(flatten(textBlock(ok(backspace(withText, caret(id(2), 2))).doc, id(2)).children)).toBe("b");
  });

  it("Enter in an empty NESTED item goes up one level", () => {
    let d = doc(ul(10, false, li(11, p(1, t("a"))), li(12, p(2))));
    d = ok(indentListItems(d, caret(id(2), 0))).doc;
    const up = ok(enter(d, caret(id(2), 0)));
    expect(listState(up.doc, caret(id(2), 0))?.depth).toBe(1);
  });

  it("cannot make a list inside a table or a quote", () => {
    const d = doc(table(10, row(11, cell(12, p(1, t("x"))))));
    expect(toggleList(d, caret(id(1), 0), false)).toHaveProperty("blocked");
  });
});

describe("quotes", () => {
  it("wraps consecutive body blocks in one quote and unwraps again", () => {
    const d = doc(p(1, t("a")), p(2, t("b")), p(3, t("c")));
    const r = ok(toggleQuote(d, range(1, 0, 2, 1)));
    expect(r.doc.blocks.map((b) => b.type)).toEqual(["quote", "paragraph"]);
    const back = ok(toggleQuote(r.doc, range(1, 0, 2, 1)));
    expect(back.doc.blocks.map((b) => b.type)).toEqual(["paragraph", "paragraph", "paragraph"]);
    expect(back.doc.blocks.map((b) => b.id)).toEqual([id(1), id(2), id(3)]);
  });

  it("turns a heading into a paragraph when quoting it", () => {
    const r = ok(toggleQuote(doc({ type: "heading", id: id(1), level: 1, children: [t("H")] }), caret(id(1), 0)));
    expect((r.doc.blocks[0] as never as { children: { type: string }[] }).children[0]!.type).toBe("paragraph");
  });
});

describe("tables", () => {
  const base = doc(p(1, t("before")), p(2, t("after")));

  it("inserts a table after the current block with the caret in the first cell", () => {
    const r = ok(insertTable(base, caret(id(1), 0), 2, 3, true));
    expect(r.doc.blocks.map((b) => b.type)).toEqual(["paragraph", "table", "paragraph"]);
    const ctx = tableContext(r.doc, r.sel.focus.id)!;
    expect([ctx.rows, ctx.cols, ctx.rowIndex, ctx.colIndex]).toEqual([2, 3, 0, 0]);
    expect(ctx.table.children[0]!.children.every((c) => c.header)).toBe(true);
    expect(ctx.table.children[1]!.children.some((c) => c.header)).toBe(false);
  });

  it("always leaves a paragraph after a table at the end of the document", () => {
    const r = ok(insertTable(doc(p(1, t("only"))), caret(id(1), 0), 1, 1, false));
    expect(r.doc.blocks.map((b) => b.type)).toEqual(["paragraph", "table", "paragraph"]);
  });

  it("refuses nesting and out-of-range sizes", () => {
    const inCell = ok(insertTable(base, caret(id(1), 0), 1, 1, false));
    expect(insertTable(inCell.doc, inCell.sel, 2, 2, false)).toHaveProperty("blocked");
    expect(insertTable(base, caret(id(1), 0), 51, 1, false)).toHaveProperty("blocked");
    expect(insertTable(base, caret(id(1), 0), 1, 21, false)).toHaveProperty("blocked");
    expect(insertTable(base, caret(id(1), 0), 0, 1, false)).toHaveProperty("blocked");
  });

  it("adds and removes rows and columns; the last row and column cannot be removed", () => {
    let r = ok(insertTable(base, caret(id(1), 0), 2, 2, false));
    r = ok(addRow(r.doc, r.sel, "below"));
    expect(tableContext(r.doc, r.sel.focus.id)!.rows).toBe(3);
    r = ok(addColumn(r.doc, r.sel, "right"));
    expect(tableContext(r.doc, r.sel.focus.id)!.cols).toBe(3);
    r = ok(removeColumn(r.doc, r.sel));
    r = ok(removeRow(r.doc, r.sel));
    expect(tableContext(r.doc, r.sel.focus.id)).toMatchObject({ rows: 2, cols: 2 });
    let small = ok(insertTable(base, caret(id(1), 0), 1, 1, false));
    expect(removeRow(small.doc, small.sel)).toHaveProperty("blocked");
    expect(removeColumn(small.doc, small.sel)).toHaveProperty("blocked");
    expect(small).toBeTruthy();
  });

  it("new columns copy the header flag of their row; rows are rectangular afterwards", () => {
    let r = ok(insertTable(base, caret(id(1), 0), 2, 2, true));
    r = ok(addColumn(r.doc, r.sel, "left"));
    const ctx = tableContext(r.doc, r.sel.focus.id)!;
    expect(ctx.table.children[0]!.children.map((c) => !!c.header)).toEqual([true, true, true]);
    expect(ctx.table.children[1]!.children.map((c) => !!c.header)).toEqual([false, false, false]);
  });

  it("enforces the 50 x 20 limits", () => {
    let r = ok(insertTable(base, caret(id(1), 0), 50, 20, false));
    expect(addRow(r.doc, r.sel, "below")).toHaveProperty("blocked");
    expect(addColumn(r.doc, r.sel, "right")).toHaveProperty("blocked");
    r = ok(insertTable(base, caret(id(1), 0), 1, 1, false));
    expect(r).toBeTruthy();
  });

  it("deletes the whole table and puts the caret back above it", () => {
    const r = ok(insertTable(base, caret(id(1), 0), 2, 2, false));
    const gone = ok(deleteTable(r.doc, r.sel));
    expect(gone.doc.blocks.map((b) => b.type)).toEqual(["paragraph", "paragraph"]);
    expect(gone.sel).toEqual(caret(id(1), 6));
    const onlyTable = ok(deleteTable(doc(table(10, row(11, cell(12, p(5))))), caret(id(5), 0)));
    expect(onlyTable.doc.blocks).toHaveLength(1); // a document is never empty
  });

  it("Tab walks the cells in order, appends a row from the last cell; Shift+Tab stops at the first", () => {
    const r = ok(insertTable(base, caret(id(1), 0), 2, 2, false));
    const cellIds = (d: Doc) => (d.blocks[1] as never as { children: { children: { children: { id: string }[] }[] }[] }).children.flatMap((row) => row.children.map((c) => c.children[0]!.id));
    const ids = cellIds(r.doc);
    let cur = r;
    for (let i = 1; i < 4; i++) {
      cur = ok(tableTab(cur.doc, cur.sel, false));
      expect(cur.sel.focus.id).toBe(ids[i]);
    }
    const grown = ok(tableTab(cur.doc, cur.sel, false)); // last cell: a new row
    expect(tableContext(grown.doc, grown.sel.focus.id)).toMatchObject({ rows: 3, rowIndex: 2, colIndex: 0 });
    const back = ok(tableTab(grown.doc, grown.sel, true));
    expect(back.sel.focus.id).toBe(ids[3]);
    const first = ok(tableTab(r.doc, r.sel, true));
    expect(first.sel.focus.id).toBe(ids[0]);
    expect(first.doc).toBe(r.doc); // nothing changed
    expect(tableTab(base, caret(id(1), 0), false)).toBeNull(); // not in a table
  });
});

describe("typing over a formatted selection and font notices", () => {
  it("text typed over a selection takes the formatting of the first selected character", async () => {
    const { insertText } = await import("../src/editor/commands");
    const d = doc(p(1, t("hello "), t("world", { bold: true })));
    const r = ok(insertText(d, range(1, 6, 1, 11), "there"));
    expect(kids(r.doc, 1)).toEqual([t("hello "), t("there", { bold: true })]);
    const plain = ok(insertText(doc(p(1, t("ab", { bold: true }), t("cd"))), range(1, 2, 1, 4), "X"));
    expect(kids(plain.doc, 1)).toEqual([t("ab", { bold: true }), t("X")]); // first selected char "c" is plain
  });

  it("explains substituted fonts only when the document uses them", async () => {
    const { fontNotice, fontsUsed } = await import("../src/editor/fonts");
    expect(fontNotice(doc(p(1, t("plain"))))).toBeNull();
    expect(fontNotice(doc(p(1, t("a", { font: "serif" }))))).toBeNull();
    const d = doc(p(1, t("a", { font: "calibri" }), t("b", { font: "times_new_roman" })), table(10, row(11, cell(12, p(2, t("c", { font: "courier_new" }))))));
    expect([...fontsUsed(d)].sort()).toEqual(["calibri", "courier_new", "times_new_roman"]);
    const notice = fontNotice(d)!;
    expect(notice).toContain("Calibri is shown as Carlito");
    expect(notice).toContain("Times New Roman is shown as Tinos");
    expect(notice).toContain("Courier New is shown as Cousine");
    expect(notice).toMatch(/another computer/);
  });
});
