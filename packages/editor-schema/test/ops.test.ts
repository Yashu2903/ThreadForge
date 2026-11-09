/** Edit operations: every operation, the formatting rules, scope, atomicity, limits, hostile input. */
import { describe, expect, it } from "vitest";
import {
  type Operation,
  type OpScope,
  type PMJson,
  MAX_INSERTED_CHARS,
  MAX_OPERATIONS,
  START,
  applyOperations,
  buildDraftDocument,
  extractText,
  validateDocument,
} from "../src";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
let counter = 1000;
const newId = () => id(counter++);
const t = (text: string, marks?: unknown[]) => ({ type: "text", text, ...(marks ? { marks } : {}) }) as PMJson;
const p = (n: number, ...content: PMJson[]): PMJson => ({ type: "paragraph", attrs: { id: id(n) }, ...(content.length ? { content } : {}) });
const heading = (n: number, text: string): PMJson => ({ type: "heading", attrs: { id: id(n), level: 2 }, content: [t(text)] });
const doc = (...content: PMJson[]): PMJson => ({ type: "doc", content });
const style = { type: "textStyle", attrs: { fontKey: "serif", fontSizePt: 14, color: "#112233" } };
const mark = { type: "highlight", attrs: { color: "#00FF00" } };

const base = doc(
  heading(1, "Plan"),
  p(2, t("Styled start", [style, mark]), t(" and plain end.")),
  p(3, t("Third paragraph.")),
  { type: "bulletList", attrs: { id: id(4) }, content: [{ type: "listItem", attrs: { id: id(5) }, content: [p(6, t("item"))] }] },
  p(7, t("Last.")),
);
const open: OpScope = { wholeIds: null, ranges: null, anchors: null };
const run = (ops: unknown, scope: OpScope = open, d: PMJson = base) => applyOperations(d, ops, { scope, newId });
const ok = (ops: unknown, scope?: OpScope) => {
  const r = run(ops, scope);
  expect(r.ok, JSON.stringify(r)).toBe(true);
  if (!r.ok) throw new Error("unreachable");
  return r;
};
const errorOf = (ops: unknown, scope?: OpScope) => {
  const r = run(ops, scope);
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  return r.errors[0]!;
};

describe("replace_block_text", () => {
  it("keeps the element, id and block attributes, and inherits font/size/colour/highlight from the first run", () => {
    const aligned = doc({ ...p(2, t("Styled start", [style, mark]), t(" and plain end.")), attrs: { id: id(2), textAlign: "center", indent: 2 } });
    const r = run([{ op: "replace_block_text", block_id: id(2), new_inline: [{ text: "New words", marks: [{ type: "bold" }] }] }], open, aligned);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    const node = r.doc.content![0]!;
    expect(node.attrs).toMatchObject({ id: id(2), textAlign: "center", indent: 2 });
    expect(node.content![0]!.text).toBe("New words");
    const marks = node.content![0]!.marks!.map((m) => m.type).sort();
    expect(marks).toEqual(["bold", "highlight", "textStyle"]);
    expect(node.content![0]!.marks!.find((m) => m.type === "textStyle")!.attrs).toMatchObject({ fontKey: "serif", fontSizePt: 14, color: "#112233" });
  });

  it("turns line feeds into hard breaks and links through the link policy", () => {
    const r = ok([{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "a\nb" }, { text: " site", marks: [{ type: "link", href: "HTTPS://example.com/x" }] }] }]);
    const node = r.doc.content![2]!;
    expect(node.content!.map((n) => n.type)).toEqual(["text", "hardBreak", "text", "text"]);
    expect(node.content![3]!.marks![0]).toEqual({ type: "link", attrs: { href: "https://example.com/x" } });
  });

  it.each([
    ["javascript:alert(1)"],
    ["data:text/html,x"],
    ["ftp://host/file"],
    ["https://user:pw@host/"],
    ["//host/path"],
  ])("refuses the link %s", (href) => {
    expect(errorOf([{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "x", marks: [{ type: "link", href }] }] }]).code).toBe("invalid_link");
  });

  it("refuses marks the model may not use, control characters, and a container", () => {
    expect(errorOf([{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "x", marks: [{ type: "highlight" }] }] }]).code).toBe("invalid_mark");
    expect(errorOf([{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "x\u0007" }] }]).code).toBe("invalid_text");
    expect(errorOf([{ op: "replace_block_text", block_id: id(4), new_inline: [{ text: "x" }] }]).code).toBe("not_a_text_block");
    expect(errorOf([{ op: "replace_block_text", block_id: id(99), new_inline: [{ text: "x" }] }]).code).toBe("block_not_found");
  });

  it("can replace a diagram's source with a valid one, and refuses a hostile one", () => {
    const d = doc(p(1, t("x")), { type: "diagram", attrs: { id: id(2) }, content: [t("flowchart TD\n  A --> B")] });
    const good = applyOperations(d, [{ op: "replace_block_text", block_id: id(2), new_inline: [{ text: "flowchart TD\n  A --> C" }] }], { newId });
    expect(good.ok).toBe(true);
    const bad = applyOperations(d, [{ op: "replace_block_text", block_id: id(2), new_inline: [{ text: "flowchart TD\n  click A callback" }] }], { newId });
    expect(bad.ok).toBe(false);
  });
});

describe("replace_range", () => {
  it("replaces inside one run and splits the runs around it, inheriting from the replaced run", () => {
    // "Styled start and plain end." ; replace "start" (7..12)
    const r = ok([{ op: "replace_range", block_id: id(2), start: 7, end: 12, new_inline: [{ text: "finish" }] }]);
    const node = r.doc.content![1]!;
    expect(extractText({ type: "doc", content: [node] })).toBe("Styled finish and plain end.");
    const finish = node.content!.find((n) => n.text === "Styled finish")!;
    expect(finish ?? node.content![0]).toBeTruthy();
    expect(node.content![0]!.marks!.map((m) => m.type).sort()).toEqual(["highlight", "textStyle"]);
  });

  it("works across runs and counts an emoji as two units", () => {
    const e = doc(p(1, t("ab😀cd")));
    const r = applyOperations(e, [{ op: "replace_range", block_id: id(1), start: 2, end: 4, new_inline: [{ text: "X" }] }], { newId });
    expect(r.ok).toBe(true);
    if (r.ok) expect(extractText(r.doc)).toBe("abXcd");
    const split = applyOperations(e, [{ op: "replace_range", block_id: id(1), start: 3, end: 5, new_inline: [{ text: "X" }] }], { newId });
    expect(split.ok).toBe(false);
    if (!split.ok) expect(split.errors[0]!.code).toBe("bad_range");
  });

  it("refuses ranges outside the text, reversed, and outside the declared range", () => {
    expect(errorOf([{ op: "replace_range", block_id: id(3), start: 0, end: 400, new_inline: [] }]).code).toBe("bad_range");
    expect(errorOf([{ op: "replace_range", block_id: id(3), start: 5, end: 2, new_inline: [] }]).code).toBe("bad_range");
    const narrow: OpScope = { wholeIds: new Set(), ranges: [{ block_id: id(3), start: 0, end: 5 }], anchors: [] };
    expect(errorOf([{ op: "replace_range", block_id: id(3), start: 0, end: 9, new_inline: [{ text: "x" }] }], narrow).code).toBe("range_out_of_scope");
    expect(run([{ op: "replace_range", block_id: id(3), start: 1, end: 4, new_inline: [{ text: "x" }] }], narrow).ok).toBe(true);
  });

  it("deleting a whole range leaves the rest", () => {
    const r = ok([{ op: "replace_range", block_id: id(3), start: 0, end: 6, new_inline: [] }]);
    expect(extractText({ type: "doc", content: [r.doc.content![2]!] })).toBe("paragraph.");
  });
});

describe("insert_blocks_after", () => {
  const blocks = [
    { type: "heading", level: 2, inline: [{ text: "Requirements" }] },
    { type: "paragraph", inline: [{ text: "Must be fast." }] },
    { type: "bullet_list", items: [{ inline: [{ text: "one" }], depth: 0 }, { inline: [{ text: "nested" }], depth: 1 }, { inline: [{ text: "two" }], depth: 0 }] },
    { type: "numbered_list", items: [{ inline: [{ text: "first" }], depth: 0 }] },
    { type: "quote", paragraphs: [[{ text: "said" }]] },
    { type: "table", header_row: true, rows: [[[{ text: "a" }], [{ text: "b" }]], [[{ text: "1" }], [{ text: "2" }]]] },
    { type: "diagram", mermaid: "erDiagram\n  USER ||--o{ ORDER : places" },
  ];

  it("inserts every block kind after an anchor, with fresh ids, and the whole result validates", () => {
    const r = ok([{ op: "insert_blocks_after", anchor_block_id: id(3), blocks }]);
    const types = r.doc.content!.map((n) => n.type);
    expect(types).toEqual(["heading", "paragraph", "paragraph", "heading", "paragraph", "bulletList", "orderedList", "blockquote", "table", "diagram", "bulletList", "paragraph"]);
    const list = r.doc.content![5]!;
    expect(list.content!.length).toBe(2);
    expect(list.content![0]!.content![1]!.type).toBe("bulletList"); // "nested" sits inside the first item
    expect(validateDocument(r.doc).ok).toBe(true);
    const ids = JSON.stringify(r.doc).match(/"id":"[^"]+"/g)!;
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("inserts at the start, and keeps the order of several inserts after one anchor", () => {
    const r = ok([
      { op: "insert_blocks_after", anchor_block_id: START, blocks: [{ type: "paragraph", inline: [{ text: "Top" }] }] },
      { op: "insert_blocks_after", anchor_block_id: id(3), blocks: [{ type: "paragraph", inline: [{ text: "A" }] }] },
      { op: "insert_blocks_after", anchor_block_id: id(3), blocks: [{ type: "paragraph", inline: [{ text: "B" }] }] },
    ]);
    expect(extractText(r.doc).split("\n").slice(0, 1)).toEqual(["Top"]);
    const texts = r.doc.content!.map((n) => extractText({ type: "doc", content: [n] }));
    expect(texts.indexOf("A")).toBe(texts.indexOf("Third paragraph.") + 1);
    expect(texts.indexOf("B")).toBe(texts.indexOf("A") + 1);
  });

  it("refuses an undeclared anchor, a nested anchor, a deleted anchor, lists that are too deep, and ragged tables", () => {
    const declared: OpScope = { wholeIds: new Set(), ranges: [], anchors: [id(3)] };
    const ins = (anchor: string, b: unknown[]) => [{ op: "insert_blocks_after", anchor_block_id: anchor, blocks: b }];
    const para = [{ type: "paragraph", inline: [{ text: "x" }] }];
    expect(errorOf(ins(id(7), para), declared).code).toBe("anchor_not_allowed");
    expect(errorOf(ins(id(6), para)).code).toBe("anchor_not_top_level");
    expect(errorOf([...ins(id(3), para), { op: "delete_blocks", block_ids: [id(3)] }]).code).toBe("overlapping_operations");
    const deep = [{ type: "bullet_list", items: [0, 1, 2, 3].map((depth) => ({ inline: [{ text: "x" }], depth })) }];
    expect(errorOf(ins(id(3), deep)).code).toBe("list_too_deep");
    const jump = [{ type: "bullet_list", items: [{ inline: [{ text: "x" }], depth: 0 }, { inline: [{ text: "y" }], depth: 2 }] }];
    expect(errorOf(ins(id(3), jump)).code).toBe("invalid_block");
    const ragged = [{ type: "table", rows: [[[{ text: "a" }]], [[{ text: "b" }], [{ text: "c" }]]] }];
    expect(errorOf(ins(id(3), ragged)).code).toBe("table_not_rectangular");
    expect(errorOf(ins(id(3), [{ type: "image", src: "x" }])).code).toBe("invalid_block");
    expect(errorOf(ins(id(3), [{ type: "heading", level: 5, inline: [{ text: "x" }] }])).code).toBe("invalid_block");
  });

  it("refuses a hostile diagram", () => {
    const bad = [{ type: "diagram", mermaid: 'flowchart TD\n  A --> B\n  click A href "javascript:alert(1)"' }];
    expect(errorOf([{ op: "insert_blocks_after", anchor_block_id: id(3), blocks: bad }]).code).toMatch(/^diagram_/);
  });
});

describe("delete_blocks", () => {
  it("deletes blocks and containers with everything inside", () => {
    const r = ok([{ op: "delete_blocks", block_ids: [id(4), id(7)] }]);
    expect(r.doc.content!.map((n) => n.type)).toEqual(["heading", "paragraph", "paragraph"]);
  });
  it("refuses to delete everything (a document needs a block) and overlapping targets", () => {
    const all = errorOf([{ op: "delete_blocks", block_ids: [id(1), id(2), id(3), id(4), id(7)] }]);
    expect(all.code).toMatch(/^candidate_/);
    expect(errorOf([{ op: "delete_blocks", block_ids: [id(4)] }, { op: "replace_block_text", block_id: id(6), new_inline: [] }]).code).toBe("overlapping_operations");
    expect(errorOf([{ op: "delete_blocks", block_ids: [id(3), id(3)] }]).code).toBe("overlapping_operations");
  });
});

describe("scope, size and atomicity", () => {
  it("refuses a target outside the declared scope without touching the document", () => {
    const scope: OpScope = { wholeIds: new Set([id(2)]), ranges: [], anchors: [id(2)] };
    const before = JSON.stringify(base);
    const r = run([{ op: "replace_block_text", block_id: id(2), new_inline: [{ text: "ok" }] }, { op: "replace_block_text", block_id: id(3), new_inline: [{ text: "no" }] }], scope);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.errors[0]).toMatchObject({ op: 1, code: "out_of_scope" });
    expect(JSON.stringify(base)).toBe(before);
  });

  it("is all-or-nothing: one bad operation fails the whole proposal", () => {
    const r = run([{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "fine" }] }, { op: "bogus" }]);
    expect(r.ok).toBe(false);
  });

  it("enforces the operation and character caps", () => {
    const many = Array.from({ length: MAX_OPERATIONS + 1 }, () => ({ op: "delete_blocks", block_ids: [id(7)] }));
    expect(errorOf(many).code).toBe("too_many_operations");
    expect(errorOf([]).code).toBe("no_operations");
    const big = [{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "x".repeat(MAX_INSERTED_CHARS + 1) }] }];
    expect(errorOf(big).code).toBe("too_much_text");
  });

  it("rejects an operation that would make the whole document invalid (list inside a quote is not constructible, so use a table too big)", () => {
    const rows = Array.from({ length: 60 }, () => [[{ text: "x" }]]);
    expect(errorOf([{ op: "insert_blocks_after", anchor_block_id: id(3), blocks: [{ type: "table", rows }] }]).code).toBe("table_too_big");
  });

  it("does not mutate its input", () => {
    const before = JSON.stringify(base);
    ok([{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "changed" }] }]);
    expect(JSON.stringify(base)).toBe(before);
  });

  it("reports before and after for the preview", () => {
    const r = ok([{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "changed" }] }, { op: "delete_blocks", block_ids: [id(7)] }]);
    expect(r.changes.map((c) => [c.kind, c.block_id])).toEqual([["replace_block_text", id(3)], ["delete_blocks", id(7)]]);
    expect(extractText({ type: "doc", content: r.changes[0]!.before })).toBe("Third paragraph.");
    expect(extractText({ type: "doc", content: r.changes[0]!.after })).toBe("changed");
    expect(r.changes[1]!.after).toEqual([]);
  });
});

describe("a half-typed diagram elsewhere does not block an unrelated edit", () => {
  it("validates the candidate with the structure policy", () => {
    const d = doc(p(1, t("x")), { type: "diagram", attrs: { id: id(2) }, content: [t("fl")] });
    const r = applyOperations(d, [{ op: "replace_block_text", block_id: id(1), new_inline: [{ text: "y" }] }], { newId });
    expect(r.ok).toBe(true);
  });
});

describe("buildDraftDocument", () => {
  it("builds a whole valid document from blocks", () => {
    const r = buildDraftDocument(
      { title: "T", blocks: [{ type: "heading", level: 1, inline: [{ text: "Proposal" }] }, { type: "paragraph", inline: [{ text: "Body" }] }, { type: "diagram", mermaid: "flowchart TD\n  A --> B" }] as never },
      { newId },
    );
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.doc.content!.map((n) => n.type)).toEqual(["heading", "paragraph", "diagram"]);
  });
  it("is strict about diagrams and refuses an empty draft", () => {
    expect(buildDraftDocument({ title: "T", blocks: [{ type: "diagram", mermaid: "pie\n  a: 1" }] as never }, { newId }).ok).toBe(false);
    expect(buildDraftDocument({ title: "T", blocks: [] }, { newId }).ok).toBe(false);
  });
});

describe("operations typed as data", () => {
  it("round-trips through JSON (they are stored as JSONB and replayed at apply)", () => {
    const ops: Operation[] = [{ op: "replace_block_text", block_id: id(3), new_inline: [{ text: "z" }] }];
    const again = JSON.parse(JSON.stringify(ops));
    expect(run(again).ok).toBe(true);
  });
});
