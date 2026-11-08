import { describe, expect, it } from "vitest";
import { validateDocument } from "threadforge-client/document-schema";
import type { Doc } from "../src/editor/doc";
import { indexOf, textBlock } from "../src/editor/doc";
import {
  type Outcome,
  type Sel,
  caret,
  deleteBackward,
  deleteForward,
  insertHardBreak,
  insertPlainText,
  insertText,
  selectionText,
  splitBlock,
  deleteSelection,
} from "../src/editor/commands";
import { flatten } from "../src/editor/inline";
import { docStats, limitProblem } from "../src/editor/stats";
import { DEFAULT_LIMITS } from "threadforge-client/document-schema";
import { hasSegmenter, useFallbackSegmentation } from "../src/editor/segment";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const t = (text: string, marks?: object) => ({ type: "text" as const, text, ...(marks ? { marks } : {}) });
const p = (n: number, ...children: object[]) => ({ type: "paragraph" as const, id: id(n), ...(children.length ? { children } : {}) });
const doc = (...blocks: object[]): Doc => ({ schema_version: 1, blocks: blocks as never });
const li = (n: number, ...children: object[]) => ({ type: "list_item", id: id(n), children });
const ul = (n: number, ...items: object[]) => ({ type: "list", id: id(n), children: items });
const cell = (n: number, ...ps: object[]) => ({ type: "table_cell", id: id(n), children: ps });
const row = (n: number, ...cells: object[]) => ({ type: "table_row", id: id(n), children: cells });
const table = (n: number, ...rows: object[]) => ({ type: "table", id: id(n), children: rows });

const ok = (r: Outcome): { doc: Doc; sel: Sel } => {
  expect(r).not.toBeNull();
  expect(r).not.toHaveProperty("blocked");
  return r as { doc: Doc; sel: Sel };
};
const textOf = (d: Doc, n: number) => flatten(textBlock(d, id(n)).children);
const valid = (d: Doc) => expect(() => validateDocument(d)).not.toThrow();

describe("typing", () => {
  it("inserts text at the caret and moves the caret after it", () => {
    const r = ok(insertText(doc(p(1, t("hello"))), caret(id(1), 5), " world"));
    expect(textOf(r.doc, 1)).toBe("hello world");
    expect(r.sel).toEqual(caret(id(1), 11));
    valid(r.doc);
  });

  it("inherits the marks of the character before the caret", () => {
    const r = ok(insertText(doc(p(1, t("ab", { bold: true }), t("cd"))), caret(id(1), 2), "X"));
    expect(textBlock(r.doc, id(1)).children).toEqual([t("abX", { bold: true }), t("cd")]);
  });

  it("at the start of a block takes the marks of what follows; in an empty block there are none", () => {
    expect(textBlock(ok(insertText(doc(p(1, t("ab", { italic: true }))), caret(id(1), 0), "X")).doc, id(1)).children).toEqual([t("Xab", { italic: true })]);
    expect(textBlock(ok(insertText(doc(p(1)), caret(id(1), 0), "X")).doc, id(1)).children).toEqual([t("X")]);
  });

  it("replaces a selection, including one that spans blocks", () => {
    const d = doc(p(1, t("hello")), p(2, t("big")), p(3, t("world")));
    const sel: Sel = { anchor: { id: id(1), offset: 2 }, focus: { id: id(3), offset: 3 } };
    const r = ok(insertText(d, sel, "-"));
    expect(r.doc.blocks).toHaveLength(1);
    expect(textOf(r.doc, 1)).toBe("he-ld");
    expect(r.sel).toEqual(caret(id(1), 3));
  });

  it("works with a backwards selection", () => {
    const d = doc(p(1, t("hello world")));
    const r = ok(insertText(d, { anchor: { id: id(1), offset: 8 }, focus: { id: id(1), offset: 3 } }, "|"));
    expect(textOf(r.doc, 1)).toBe("hel|rld");
  });

  it("drops control characters, newlines and lone surrogates but keeps tabs and emoji", () => {
    const r = ok(insertText(doc(p(1)), caret(id(1), 0), "a\tb\u0000c\u0085d e\ud800f😀"));
    expect(textOf(r.doc, 1)).toBe("a\tbcdef😀");
    valid(r.doc);
    expect(insertText(doc(p(1)), caret(id(1), 0), "\u0000\u0007")).toBeNull();
  });

  it("never mutates its input (structural sharing: untouched blocks are the same objects)", () => {
    const d = doc(p(1, t("a")), p(2, t("b")));
    const r = ok(insertText(d, caret(id(1), 1), "x"));
    expect(r.doc.blocks[1]).toBe(d.blocks[1]);
    expect(r.doc.blocks[0]).not.toBe(d.blocks[0]);
    expect(textOf(d, 1)).toBe("a");
  });

  it("inserts a hard break as one offset unit", () => {
    const r = ok(insertHardBreak(doc(p(1, t("ab"))), caret(id(1), 1)));
    expect(textBlock(r.doc, id(1)).children).toEqual([t("a"), { type: "hard_break" }, t("b")]);
    expect(r.sel).toEqual(caret(id(1), 2));
    valid(r.doc);
  });
});

describe("Enter (split)", () => {
  it("the first part keeps the id; the new block gets a fresh one", () => {
    const r = ok(splitBlock(doc(p(1, t("hello"))), caret(id(1), 2)));
    expect(r.doc.blocks).toHaveLength(2);
    expect(r.doc.blocks[0]!.id).toBe(id(1));
    expect(r.doc.blocks[1]!.id).not.toBe(id(1));
    expect(flatten((r.doc.blocks[0] as never as { children: never[] }).children)).toBe("he");
    expect(flatten((r.doc.blocks[1] as never as { children: never[] }).children)).toBe("llo");
    expect(r.sel).toEqual(caret(r.doc.blocks[1]!.id, 0));
    valid(r.doc);
  });

  it("at the end creates an empty paragraph with no children; at the start leaves an empty first block", () => {
    const end = ok(splitBlock(doc(p(1, t("ab"))), caret(id(1), 2)));
    expect(end.doc.blocks[1]).not.toHaveProperty("children");
    const start = ok(splitBlock(doc(p(1, t("ab"))), caret(id(1), 0)));
    expect(start.doc.blocks[0]).not.toHaveProperty("children");
    expect(start.doc.blocks[0]!.id).toBe(id(1));
  });

  it("keeps marks on both halves and the paragraph attributes on the new block", () => {
    const d = doc({ ...p(1, t("abcd", { bold: true })), attrs: { align: "center" } });
    const r = ok(splitBlock(d, caret(id(1), 2)));
    expect((r.doc.blocks[1] as never as { attrs: object }).attrs).toEqual({ align: "center" });
    expect((r.doc.blocks[0] as never as { children: object[] }).children).toEqual([t("ab", { bold: true })]);
    expect((r.doc.blocks[1] as never as { children: object[] }).children).toEqual([t("cd", { bold: true })]);
  });

  it("a heading split at its end continues as a paragraph; split in the middle stays a heading", () => {
    const h = { type: "heading", id: id(1), level: 2, children: [t("Title")] };
    const end = ok(splitBlock(doc(h), caret(id(1), 5)));
    expect(end.doc.blocks[1]!.type).toBe("paragraph");
    const mid = ok(splitBlock(doc(h), caret(id(1), 2)));
    expect(mid.doc.blocks[1]!.type).toBe("heading");
    valid(mid.doc);
  });

  it("deletes a selection first", () => {
    const r = ok(splitBlock(doc(p(1, t("abcdef"))), { anchor: { id: id(1), offset: 2 }, focus: { id: id(1), offset: 4 } }));
    expect(textOf(r.doc, 1)).toBe("ab");
    expect(flatten((r.doc.blocks[1] as never as { children: never[] }).children)).toBe("ef");
  });

  it("inside a list item it creates a new list item, moving what followed", () => {
    const d = doc(ul(10, li(11, p(1, t("one")), ul(12, li(13, p(2, t("nested")))))));
    const r = ok(splitBlock(d, caret(id(1), 1)));
    const list = r.doc.blocks[0] as never as { children: { id: string; children: { type: string }[] }[] };
    expect(list.children).toHaveLength(2);
    expect(list.children[0]!.id).toBe(id(11));
    expect(list.children[0]!.children).toHaveLength(1);
    expect(list.children[1]!.children.map((c) => c.type)).toEqual(["paragraph", "list"]);
    valid(r.doc);
  });

  it("inside a table cell it makes another paragraph in the same cell", () => {
    const d = doc(table(10, row(11, cell(12, p(1, t("ab"))), cell(13, p(2)))));
    const r = ok(splitBlock(d, caret(id(1), 1)));
    const c = (r.doc.blocks[0] as never as { children: { children: { children: object[] }[] }[] }).children[0]!.children[0]!;
    expect(c.children).toHaveLength(2);
    valid(r.doc);
  });
});

describe("Backspace and Delete", () => {
  it("deletes one character, never half of an emoji or a combining sequence", () => {
    expect(textOf(ok(deleteBackward(doc(p(1, t("a😀"))), caret(id(1), 3))).doc, 1)).toBe("a");
    expect(textOf(ok(deleteBackward(doc(p(1, t("éx"))), caret(id(1), 2))).doc, 1)).toBe("x");
    expect(textOf(ok(deleteForward(doc(p(1, t("😀b"))), caret(id(1), 0))).doc, 1)).toBe("b");
  });

  it("deletes a whole ZWJ family emoji, a flag and a skin-tone emoji as one unit", () => {
    const family = "\u{1F468}‍\u{1F469}‍\u{1F467}";
    expect(textOf(ok(deleteBackward(doc(p(1, t(`x${family}`))), caret(id(1), 1 + family.length))).doc, 1)).toBe("x");
    expect(textOf(ok(deleteBackward(doc(p(1, t("x\u{1F1EB}\u{1F1F7}"))), caret(id(1), 5))).doc, 1)).toBe("x");
    expect(textOf(ok(deleteBackward(doc(p(1, t("x\u{1F44D}\u{1F3FD}"))), caret(id(1), 5))).doc, 1)).toBe("x");
  });

  it("the grapheme fallback (no Intl.Segmenter) handles the same cases", () => {
    expect(hasSegmenter()).toBe(true);
    useFallbackSegmentation(true);
    try {
      expect(hasSegmenter()).toBe(false);
      const family = "\u{1F468}‍\u{1F469}‍\u{1F467}";
      expect(textOf(ok(deleteBackward(doc(p(1, t(`x${family}`))), caret(id(1), 1 + family.length))).doc, 1)).toBe("x");
      expect(textOf(ok(deleteBackward(doc(p(1, t("é"))), caret(id(1), 2))).doc, 1)).toBe("");
      expect(textOf(ok(deleteBackward(doc(p(1, t("x\u{1F1EB}\u{1F1F7}"))), caret(id(1), 5))).doc, 1)).toBe("x");
      expect(textOf(ok(deleteForward(doc(p(1, t("a😀b"))), caret(id(1), 1))).doc, 1)).toBe("ab");
    } finally {
      useFallbackSegmentation(false);
    }
  });

  it("deletes an inline atom as one unit", () => {
    const d = doc(p(1, t("a"), { type: "hard_break" }, t("b")));
    expect(textBlock(ok(deleteBackward(d, caret(id(1), 2))).doc, id(1)).children).toEqual([t("ab")]);
    expect(textBlock(ok(deleteForward(d, caret(id(1), 1))).doc, id(1)).children).toEqual([t("ab")]);
  });

  it("word deletion", () => {
    const d = doc(p(1, t("hello big world")));
    expect(textOf(ok(deleteBackward(d, caret(id(1), 9), "word")).doc, 1)).toBe("hello  world");
    expect(textOf(ok(deleteBackward(d, caret(id(1), 15), "word")).doc, 1)).toBe("hello big ");
    expect(textOf(ok(deleteForward(d, caret(id(1), 0), "word")).doc, 1)).toBe("big world");
  });

  it("line deletion stops at a hard break", () => {
    const d = doc(p(1, t("one"), { type: "hard_break" }, t("two")));
    expect(textBlock(ok(deleteBackward(d, caret(id(1), 7), "line")).doc, id(1)).children).toEqual([t("one"), { type: "hard_break" }]);
  });

  it("Backspace at the start joins onto the previous block; the earlier id and type win", () => {
    const d = doc({ type: "heading", id: id(1), level: 1, children: [t("Head")] }, p(2, t("tail")));
    const r = ok(deleteBackward(d, caret(id(2), 0)));
    expect(r.doc.blocks).toHaveLength(1);
    expect(r.doc.blocks[0]).toMatchObject({ type: "heading", id: id(1) });
    expect(textOf(r.doc, 1)).toBe("Headtail");
    expect(r.sel).toEqual(caret(id(1), 4));
    valid(r.doc);
  });

  it("Delete at the end pulls the next block up", () => {
    const r = ok(deleteForward(doc(p(1, t("ab")), p(2, t("cd"))), caret(id(1), 2)));
    expect(r.doc.blocks).toHaveLength(1);
    expect(textOf(r.doc, 1)).toBe("abcd");
    expect(r.sel).toEqual(caret(id(1), 2));
  });

  it("is a no-op at the very start / end of the document", () => {
    expect(deleteBackward(doc(p(1, t("a"))), caret(id(1), 0))).toBeNull();
    expect(deleteForward(doc(p(1, t("a"))), caret(id(1), 1))).toBeNull();
  });

  it("does not merge across table cells", () => {
    const d = doc(table(10, row(11, cell(12, p(1, t("ab"))), cell(13, p(2, t("cd"))))));
    expect(deleteBackward(d, caret(id(2), 0))).toBeNull();
    expect(deleteForward(d, caret(id(1), 2))).toBeNull();
  });

  it("does merge two paragraphs inside the same cell", () => {
    const d = doc(table(10, row(11, cell(12, p(1, t("ab")), p(3, t("cd"))))));
    const r = ok(deleteBackward(d, caret(id(3), 0)));
    expect(textOf(r.doc, 1)).toBe("abcd");
    valid(r.doc);
  });

  it("the first paragraph of a list item is left to the list commands (no-op here)", () => {
    const d = doc(p(1, t("before")), ul(10, li(11, p(2, t("item")))));
    expect(deleteBackward(d, caret(id(2), 0))).toBeNull();
  });

  it("a paragraph after a list joins the last list item; emptied containers are removed", () => {
    const d = doc(ul(10, li(11, p(1, t("item")))), p(2, t("after")));
    const r = ok(deleteBackward(d, caret(id(2), 0)));
    expect(r.doc.blocks).toHaveLength(1);
    expect(textOf(r.doc, 1)).toBe("itemafter");
    valid(r.doc);
  });

  it("deleting the only content of a list item's paragraph by joining removes the emptied item", () => {
    const d = doc(p(1, t("keep")), ul(10, li(11, p(2))));
    const r = ok(deleteForward(d, caret(id(1), 4)));
    expect(r.doc.blocks).toHaveLength(1); // the list vanished with its only item
    valid(r.doc);
  });
});

describe("deleting a cross-block selection", () => {
  it("removes the blocks in between and merges the ends into the first block", () => {
    const d = doc(p(1, t("alpha")), p(2, t("beta")), p(3, t("gamma")), p(4, t("delta")));
    const r = deleteSelection(d, { anchor: { id: id(1), offset: 2 }, focus: { id: id(4), offset: 2 } });
    const out = r as { doc: Doc };
    expect(out.doc.blocks).toHaveLength(1);
    expect(textOf(out.doc, 1)).toBe("allta");
  });

  it("across list items and out of the list", () => {
    const d = doc(p(1, t("intro")), ul(10, li(11, p(2, t("one"))), li(12, p(3, t("two")))), p(4, t("outro")));
    const out = deleteSelection(d, { anchor: { id: id(1), offset: 2 }, focus: { id: id(3), offset: 1 } }) as { doc: Doc };
    expect(textOf(out.doc, 1)).toBe("inwo");
    expect(out.doc.blocks.map((b) => b.id)).toEqual([id(1), id(4)]);
    valid(out.doc);
  });

  it("keeps the nested list of a removed list item's paragraph (the item gets an empty first paragraph)", () => {
    const d = doc(p(1, t("abc")), ul(10, li(11, p(2, t("xyz")), ul(12, li(13, p(3, t("deep")))))));
    const out = deleteSelection(d, { anchor: { id: id(1), offset: 1 }, focus: { id: id(2), offset: 1 } }) as { doc: Doc };
    expect(textOf(out.doc, 1)).toBe("ayz");
    expect(textOf(out.doc, 3)).toBe("deep"); // not lost
    valid(out.doc);
  });

  it("a selection inside ONE cell can be deleted; across cells it is blocked with a hint", () => {
    const d = doc(table(10, row(11, cell(12, p(1, t("ab")), p(3, t("cd"))), cell(13, p(2, t("ef"))))));
    const inside = deleteSelection(d, { anchor: { id: id(1), offset: 1 }, focus: { id: id(3), offset: 1 } }) as { doc: Doc };
    expect(textOf(inside.doc, 1)).toBe("ad");
    const across = deleteSelection(d, { anchor: { id: id(1), offset: 1 }, focus: { id: id(2), offset: 1 } });
    expect(across).toHaveProperty("blocked");
    expect(insertText(d, { anchor: { id: id(1), offset: 1 }, focus: { id: id(2), offset: 1 } }, "x")).toHaveProperty("blocked");
    const around = doc(p(5, t("top")), table(10, row(11, cell(12, p(1, t("ab"))))), p(6, t("bottom")));
    expect(deleteSelection(around, { anchor: { id: id(5), offset: 1 }, focus: { id: id(6), offset: 1 } })).toHaveProperty("blocked");
  });

  it("deleting everything leaves one empty paragraph (a document is never empty)", () => {
    const d = doc(p(1, t("a")), p(2, t("b")));
    const out = deleteSelection(d, { anchor: { id: id(1), offset: 0 }, focus: { id: id(2), offset: 1 } }) as { doc: Doc };
    expect(out.doc.blocks).toHaveLength(1);
    expect(out.doc.blocks[0]).toMatchObject({ id: id(1), type: "paragraph" });
    valid(out.doc);
  });
});

describe("plain-text paste and copy", () => {
  it("one line is inserted at the caret", () => {
    expect(textOf(ok(insertPlainText(doc(p(1, t("ad"))), caret(id(1), 1), "bc")).doc, 1)).toBe("abcd");
  });

  it("lines become paragraphs; CRLF and CR are normalized", () => {
    const r = ok(insertPlainText(doc(p(1, t("ad"))), caret(id(1), 1), "b\r\nc\rd\ne"));
    expect(r.doc.blocks).toHaveLength(4);
    expect(r.doc.blocks.map((b) => flatten((b as never as { children: never[] }).children))).toEqual(["ab", "c", "d", "ed"]);
    valid(r.doc);
  });

  it("inside a list item every line becomes a list item", () => {
    const r = ok(insertPlainText(doc(ul(10, li(11, p(1, t("x"))))), caret(id(1), 1), "a\nb\nc"));
    const list = r.doc.blocks[0] as never as { children: object[] };
    expect(list.children).toHaveLength(3);
  });

  it("inside a cell every line becomes a paragraph of that cell", () => {
    const r = ok(insertPlainText(doc(table(10, row(11, cell(12, p(1, t("x")))))), caret(id(1), 1), "a\nb"));
    const c = (r.doc.blocks[0] as never as { children: { children: { children: object[] }[] }[] }).children[0]!.children[0]!;
    expect(c.children).toHaveLength(2);
  });

  it("replaces a selection, blank lines make empty paragraphs, and invalid characters are dropped", () => {
    const r = ok(insertPlainText(doc(p(1, t("abcdef"))), { anchor: { id: id(1), offset: 1 }, focus: { id: id(1), offset: 5 } }, "X\n\nY\u0000"));
    expect(r.doc.blocks.map((b) => flatten((b as never as { children?: never[] }).children))).toEqual(["aX", "", "Yf"]);
    valid(r.doc);
  });

  it("copy text: blocks by newline, hard breaks as newline, cells by tab and rows by newline", () => {
    const d = doc(p(1, t("one"), { type: "hard_break" }, t("two")), p(2, t("three")));
    expect(selectionText(d, { anchor: { id: id(1), offset: 1 }, focus: { id: id(2), offset: 2 } })).toBe("ne\ntwo\nth");
    const tab = doc(table(10, row(11, cell(12, p(1, t("a"))), cell(13, p(2, t("b")))), row(14, cell(15, p(3, t("c"))), cell(16, p(4, t("d"))))));
    expect(selectionText(tab, { anchor: { id: id(1), offset: 0 }, focus: { id: id(4), offset: 1 } })).toBe("a\tb\nc\td");
  });
});

describe("limits", () => {
  it("counts text in code points and ignores edits that do not make things worse", () => {
    const d = doc(p(1, t("😀😀")));
    expect(docStats(d).textCp).toBe(2);
    const small = { ...DEFAULT_LIMITS, maxTextCodepoints: 2 };
    const bigger = ok(insertText(d, caret(id(1), 4), "x")).doc;
    expect(limitProblem(d, bigger, small)).toMatch(/limited to 2 characters/);
    const smaller = ok(deleteBackward(d, caret(id(1), 4))).doc;
    expect(limitProblem(bigger, smaller, small)).toBeNull();
  });

  it("indexes text blocks in document order, including nested ones", () => {
    const d = doc(p(1), ul(10, li(11, p(2), ul(12, li(13, p(3))))), table(20, row(21, cell(22, p(4)))));
    expect(indexOf(d).order).toEqual([id(1), id(2), id(3), id(4)]);
    expect(indexOf(d).chain(id(3))).toEqual([id(10), id(11), id(12), id(13), id(3)]);
  });
});
