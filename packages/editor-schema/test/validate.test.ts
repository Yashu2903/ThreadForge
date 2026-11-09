import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { DEFAULT_LIMITS, type DocError, type PMJson, blockHash, blockHashes, canonicalStringify, canonicalize, contentHash, createExtensions, extractText, getEditorSchema, normalizeWhitespace, schemaDescription, schemaFingerprint, validateDocument } from "../src";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const t = (text: string, marks?: unknown[]) => ({ type: "text", text, ...(marks ? { marks } : {}) });
const p = (n: number, content?: unknown[], attrs: object = {}) => ({ type: "paragraph", attrs: { id: id(n), ...attrs }, ...(content ? { content } : {}) });
const doc = (...content: unknown[]) => ({ type: "doc", content });
const li = (n: number, ...content: unknown[]) => ({ type: "listItem", attrs: { id: id(n) }, content });
const ul = (n: number, ...items: unknown[]) => ({ type: "bulletList", attrs: { id: id(n) }, content: items });
const cell = (n: number, ...ps: unknown[]) => ({ type: "tableCell", attrs: { id: id(n) }, content: ps });
const row = (n: number, ...cells: unknown[]) => ({ type: "tableRow", attrs: { id: id(n) }, content: cells });
const table = (n: number, ...rows: unknown[]) => ({ type: "table", attrs: { id: id(n) }, content: rows });

function errors(input: unknown, limits = DEFAULT_LIMITS): DocError[] {
  const r = validateDocument(input, limits);
  if (r.ok) throw new Error("expected the document to be rejected");
  return r.errors;
}
const first = (input: unknown, limits = DEFAULT_LIMITS) => {
  const e = errors(input, limits)[0]!;
  return `${e.path} ${e.code}`;
};

describe("accepted documents", () => {
  it("accepts what editor.getJSON() writes (null attributes, textAlign left everywhere) and canonicalizes it", () => {
    const fromEditor = doc(p(1, [t("hello", [{ type: "bold" }])], { textAlign: "left", lineSpacing: null, spaceBefore: null, spaceAfter: null, indent: null }));
    const r = validateDocument(fromEditor);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.doc).toEqual({ type: "doc", content: [{ type: "paragraph", attrs: { id: id(1) }, content: [{ type: "text", text: "hello", marks: [{ type: "bold" }] }] }] });
  });

  it("accepts every supported feature together", () => {
    const rich = doc(
      { type: "heading", attrs: { id: id(1), level: 2, textAlign: "center", lineSpacing: 1.5, spaceBefore: 6, spaceAfter: 12.5, indent: 2 }, content: [t("Title")] },
      p(2, [
        t("a", [{ type: "link", attrs: { href: "https://example.com" } }, { type: "textStyle", attrs: { fontKey: "calibri", fontSizePt: 11.5, color: "#FF0000" } }, { type: "bold" }, { type: "italic" }, { type: "strike" }, { type: "underline" }, { type: "highlight", attrs: { color: "#00FF00" } }]),
        t("b", [{ type: "superscript" }]),
        { type: "hardBreak" },
        { type: "image", attrs: { assetId: id(900), alt: "A chart", width: 100, height: 50 } },
      ]),
      { type: "blockquote", attrs: { id: id(3) }, content: [p(4, [t("quoted")])] },
      ul(5, li(6, p(7, [t("one")]), ul(8, li(9, p(10, [t("nested")]))))),
      table(11, row(12, { type: "tableHeader", attrs: { id: id(13) }, content: [p(14, [t("H")])] }, cell(15, p(16, [t("c")])))),
    );
    const r = validateDocument(rich);
    expect(r.ok, JSON.stringify(r)).toBe(true);
  });
});

describe("rejected documents carry a path", () => {
  it.each([
    ["not an object", [], " invalid_type"],
    ["not a doc", { type: "paragraph" }, " invalid_type"],
    ["unknown node", doc({ type: "div", attrs: { id: id(1) } }), "/content/0/type unknown_node"],
    ["unknown mark", doc(p(1, [t("x", [{ type: "blink" }])])), "/content/0/content/0/marks/0/type unknown_mark"],
    ["missing id", doc({ type: "paragraph" }), "/content/0/attrs/id invalid_id"],
    ["id not a uuid", doc(p(1, undefined, { id: "abc" })), "/content/0/attrs/id invalid_id"],
    ["duplicate id", doc(p(1), p(1)), "/content/1/attrs/id duplicate_id"],
    ["heading level 4", doc({ type: "heading", attrs: { id: id(1), level: 4 } }), "/content/0/attrs/level invalid_value"],
    ["bad alignment", doc(p(1, undefined, { textAlign: "start" })), "/content/0/attrs/textAlign invalid_value"],
    ["bad line spacing", doc(p(1, undefined, { lineSpacing: 1.3 })), "/content/0/attrs/lineSpacing invalid_value"],
    ["spacing over 72", doc(p(1, undefined, { spaceBefore: 73 })), "/content/0/attrs/spaceBefore invalid_value"],
    ["spacing not a half step", doc(p(1, undefined, { spaceAfter: 3.3 })), "/content/0/attrs/spaceAfter invalid_value"],
    ["indent over 8", doc(p(1, undefined, { indent: 9 })), "/content/0/attrs/indent invalid_value"],
    ["indent inside a list item", doc(ul(1, li(2, p(3, [t("x")], { indent: 1 })))), "/content/0/content/0/content/0/attrs/indent invalid_value"],
    ["font not on the allowlist", doc(p(1, [t("x", [{ type: "textStyle", attrs: { fontKey: "Comic Sans" } }])])), "/content/0/content/0/marks/0/attrs/fontKey invalid_value"],
    ["font size 7.5", doc(p(1, [t("x", [{ type: "textStyle", attrs: { fontSizePt: 7.5 } }])])), "/content/0/content/0/marks/0/attrs/fontSizePt invalid_value"],
    ["named colour", doc(p(1, [t("x", [{ type: "textStyle", attrs: { color: "red" } }])])), "/content/0/content/0/marks/0/attrs/color invalid_value"],
    ["rgb() colour", doc(p(1, [t("x", [{ type: "textStyle", attrs: { color: "rgb(1,2,3)" } }])])), "/content/0/content/0/marks/0/attrs/color invalid_value"],
    ["bad highlight colour", doc(p(1, [t("x", [{ type: "highlight", attrs: { color: "yellow" } }])])), "/content/0/content/0/marks/0/attrs/color invalid_value"],
    ["javascript link", doc(p(1, [t("x", [{ type: "link", attrs: { href: "javascript:alert(1)" } }])])), "/content/0/content/0/marks/0/attrs/href invalid_link"],
    ["data link", doc(p(1, [t("x", [{ type: "link", attrs: { href: "data:text/html,x" } }])])), "/content/0/content/0/marks/0/attrs/href invalid_link"],
    ["newline in a link", doc(p(1, [t("x", [{ type: "link", attrs: { href: "java\nscript:1" } }])])), "/content/0/content/0/marks/0/attrs/href invalid_link"],
    ["credentials in a link", doc(p(1, [t("x", [{ type: "link", attrs: { href: "https://u:p@e.com" } }])])), "/content/0/content/0/marks/0/attrs/href invalid_link"],
    ["newline in text", doc(p(1, [t("a\nb")])), "/content/0/content/0/text invalid_text"],
    ["NUL in text", doc(p(1, [t("a\u0000b")])), "/content/0/content/0/text invalid_text"],
    ["lone surrogate", doc(p(1, [t("a\ud800b")])), "/content/0/content/0/text invalid_text"],
    ["image id not a uuid", doc(p(1, [{ type: "image", attrs: { assetId: "../../etc/passwd", width: 1, height: 1 } }])), "/content/0/content/0/attrs/assetId invalid_id"],
    ["image without an asset", doc(p(1, [{ type: "image", attrs: { width: 1, height: 1 } }])), "/content/0/content/0/attrs/assetId invalid_id"],
    ["image too wide", doc(p(1, [{ type: "image", attrs: { assetId: id(9), width: 10001, height: 1 } }])), "/content/0/content/0/attrs/width invalid_value"],
    ["alt text too long", doc(p(1, [{ type: "image", attrs: { assetId: id(9), width: 1, height: 1, alt: "a".repeat(501) } }])), "/content/0/content/0/attrs/alt limit_exceeded"],
    ["list nested four deep", doc(ul(1, li(2, p(3), ul(4, li(5, p(6), ul(7, li(8, p(9), ul(10, li(11, p(12)))))))))), "/content/0/content/0/content/1/content/0/content/1/content/0/content/1 limit_exceeded"],
    ["ordered list with a custom start", doc({ type: "orderedList", attrs: { id: id(1), start: 5 }, content: [li(2, p(3))] }), "/content/0/attrs/start invalid_value"],
    ["ragged table", doc(table(1, row(2, cell(3, p(4)), cell(5, p(6))), row(7, cell(8, p(9))))), "/content/0/content/1/content invalid_structure"],
    ["merged cells (colspan)", doc(table(1, row(2, { type: "tableCell", attrs: { id: id(3), colspan: 2 }, content: [p(4)] }))), "/content/0/content/0/content/0/attrs/colspan invalid_value"],
    ["merged cells (rowspan)", doc(table(1, row(2, { type: "tableCell", attrs: { id: id(3), rowspan: 2 }, content: [p(4)] }))), "/content/0/content/0/content/0/attrs/rowspan invalid_value"],
    ["column widths", doc(table(1, row(2, { type: "tableCell", attrs: { id: id(3), colwidth: [100] }, content: [p(4)] }))), "/content/0/content/0/content/0/attrs/colwidth invalid_value"],
  ])("%s", (_name, input, expected) => {
    expect(first(input)).toBe(expected);
  });

  it("table size limits: 50 rows and 20 columns", () => {
    const rows = (n: number, cols: number) => Array.from({ length: n }, (_, r) => row(10_000 + r, ...Array.from({ length: cols }, (_, c) => cell(20_000 + r * 100 + c, p(50_000 + r * 100 + c)))));
    expect(validateDocument(doc(table(1, ...rows(50, 20)))).ok).toBe(true);
    expect(first(doc(table(1, ...rows(51, 1))))).toBe("/content/0/content limit_exceeded");
    expect(first(doc(table(1, ...rows(1, 21))))).toBe("/content/0/content/0/content limit_exceeded");
  });

  it("structure the schema forbids: a heading inside a list item, a list item that starts with a list", () => {
    const heading = { type: "heading", attrs: { id: id(3), level: 1 }, content: [t("x")] };
    expect(errors(doc(ul(1, li(2, p(4), heading))))[0]!.code).toMatch(/invalid_structure/);
    expect(errors(doc(ul(1, li(2, ul(3, li(4, p(5)))))))[0]!.code).toBe("invalid_structure");
    expect(errors(doc({ type: "blockquote", attrs: { id: id(1) }, content: [ul(2, li(3, p(4)))] }))[0]!.code).toBe("invalid_structure");
  });

  it("content the schema would silently drop is REPORTED, not accepted", () => {
    // ProseMirror ignores unknown attributes when loading JSON; validateDocument must not
    expect(first(doc(p(1, undefined, { style: "color:red" })))).toBe("/content/0/attrs/style would_be_dropped");
    expect(first(doc(p(1, [t("x", [{ type: "link", attrs: { href: "https://e.com", target: "_blank" } }])])))).toBe("/content/0/content/0/marks would_be_dropped");
    expect(first(doc(p(1, [{ type: "image", attrs: { assetId: id(9), width: 1, height: 1, src: "https://evil.example/x.png" } }])))).toBe("/content/0/content/0/attrs/src would_be_dropped");
    expect(first(doc(p(1, [{ type: "image", attrs: { assetId: id(9), width: 1, height: 1, src: "data:image/png;base64,AAAA" } }])))).toBe("/content/0/content/0/attrs/src would_be_dropped");
  });
});

describe("limits", () => {
  it("text, nodes, blocks, images and size limits are errors with paths (never truncation)", () => {
    expect(first(doc(p(1, [t("abcdef")])), { ...DEFAULT_LIMITS, maxTextCodepoints: 5 })).toBe("/content/0/content/0/text limit_exceeded");
    expect(first(doc(p(1), p(2), p(3)), { ...DEFAULT_LIMITS, maxBlocks: 2 })).toBe("/content/2 limit_exceeded");
    expect(first(doc(p(1, [t("a"), { type: "hardBreak" }])), { ...DEFAULT_LIMITS, maxTotalNodes: 3 })).toBe("/content/0/content/1 limit_exceeded");
    const img = { type: "image", attrs: { assetId: id(9), width: 1, height: 1 } };
    expect(first(doc(p(1, [img, img])), { ...DEFAULT_LIMITS, maxImages: 1 })).toBe("/content/0/content/1 limit_exceeded");
    expect(first(doc(p(1, [t("hello world")])), { ...DEFAULT_LIMITS, maxSerializedBytes: 50 })).toBe(" limit_exceeded");
  });

  it("the spec's defaults", () => {
    expect(DEFAULT_LIMITS).toMatchObject({ maxSerializedBytes: 1048576, maxTextCodepoints: 100000, maxBlocks: 5000, maxTotalNodes: 60000, maxImages: 100, maxDiagrams: 25, maxListDepth: 3, maxLinkLength: 2048, maxAltCodepoints: 500 });
  });

  it("exactly 5000 blocks pass and 5001 fail; emoji are counted as code points", () => {
    expect(validateDocument(doc(...Array.from({ length: 5000 }, (_, i) => p(i + 1)))).ok).toBe(true);
    expect(first(doc(...Array.from({ length: 5001 }, (_, i) => p(i + 1))))).toBe("/content/5000 limit_exceeded");
    const emoji = "😀".repeat(100_000);
    expect(validateDocument(doc(p(1, [t(emoji)]))).ok).toBe(true);
    expect(first(doc(p(1, [t(emoji + "a")])))).toBe("/content/0/content/0/text limit_exceeded");
  });
});

describe("canonical form, hashes and text", () => {
  it("canonicalize is idempotent and ignores key order, null attrs, defaults and mark order", () => {
    const a = { type: "doc", content: [{ type: "paragraph", attrs: { textAlign: "left", id: id(1), lineSpacing: null }, content: [{ type: "text", text: "ab", marks: [{ type: "italic" }, { type: "bold" }] }, { type: "text", text: "cd", marks: [{ type: "bold" }, { type: "italic" }] }] }] } as PMJson;
    const c = canonicalize(a);
    expect(canonicalize(c)).toEqual(c);
    expect(c.content![0]!.content).toEqual([{ type: "text", text: "abcd", marks: [{ type: "bold" }, { type: "italic" }] }]);
    expect(canonicalStringify({ b: 1, a: [2, { d: 1, c: 2 }] })).toBe('{"a":[2,{"c":2,"d":1}],"b":1}');
  });

  it("contentHash is stable under noise and changes with content", async () => {
    const a = doc(p(1, [t("x")]));
    const noisy = { type: "doc", content: [{ type: "paragraph", attrs: { id: id(1), textAlign: "left", indent: null }, content: [t("x")] }] } as PMJson;
    expect(await contentHash(a as PMJson)).toBe(await contentHash(noisy));
    expect(await contentHash(a as PMJson)).not.toBe(await contentHash(doc(p(1, [t("y")])) as PMJson));
    expect(await contentHash(a as PMJson)).toMatch(/^[0-9a-f]{64}$/);
  });

  it("blockHash describes CONTENT: same under a different id, different when the text or a mark changes", async () => {
    const h = (n: PMJson) => blockHash(n);
    const base = p(1, [t("hello")]) as PMJson;
    expect(await h(base)).toBe(await h(p(2, [t("hello")]) as PMJson));
    expect(await h(base)).not.toBe(await h(p(1, [t("hellO")]) as PMJson));
    expect(await h(base)).not.toBe(await h(p(1, [t("hello", [{ type: "bold" }])]) as PMJson));
    expect(await h(base)).not.toBe(await h(p(1, [t("hello")], { textAlign: "center" }) as PMJson));
    const list = ul(10, li(11, p(12, [t("a")]))) as PMJson;
    expect(await h(list)).toBe(await h(ul(20, li(21, p(22, [t("a")]))) as PMJson)); // nested ids are ignored too
  });

  it("blockHashes lists the top-level blocks with ids", async () => {
    const out = await blockHashes(doc(p(1, [t("a")]), p(2, [t("b")])) as PMJson);
    expect(out.map((b) => [b.id, b.type])).toEqual([[id(1), "paragraph"], [id(2), "paragraph"]]);
    expect(out[0]!.hash).not.toBe(out[1]!.hash);
  });

  it("extractText: blocks, hard breaks, list items, table cells and rows; images contribute nothing", () => {
    const d = doc(
      p(1, [t("one"), { type: "hardBreak" }, t("two")]),
      ul(2, li(3, p(4, [t("item a")])), li(5, p(6, [t("item b")]))),
      table(7, row(8, cell(9, p(10, [t("A")])), cell(11, p(12, [t("B")]))), row(13, cell(14, p(15, [t("C")])), cell(16, p(17, [t("D")])))),
      p(18, [{ type: "image", attrs: { assetId: id(900), width: 1, height: 1, alt: "ignored" } }, t("end")]),
    ) as PMJson;
    expect(extractText(d)).toBe("one\ntwo\nitem a\nitem b\nA\tB\nC\tD\nend");
    expect(normalizeWhitespace("  a \n\t b  ")).toBe("a b");
  });
});

describe("the shared schema", () => {
  it("is built from the one extension list and is stable (pinned fingerprint)", async () => {
    const description = schemaDescription() as { nodes: Record<string, unknown>; marks: Record<string, unknown> };
    expect(Object.keys(description.nodes).sort()).toEqual(["blockquote", "bulletList", "diagram", "doc", "hardBreak", "heading", "image", "listItem", "orderedList", "paragraph", "table", "tableCell", "tableHeader", "tableRow", "text"]);
    expect(Object.keys(description.marks).sort()).toEqual(["bold", "highlight", "italic", "link", "strike", "subscript", "superscript", "textStyle", "underline"]);
    // Change the schema on purpose? Update this value AND bump the collaboration data version.
    expect(await schemaFingerprint()).toBe(PINNED_FINGERPRINT);
    expect(await schemaFingerprint(getEditorSchema({}))).toBe(PINNED_FINGERPRINT);
  });

  it("does not include history (Yjs provides undo) or code blocks", () => {
    const names = createExtensions().map((e) => e.name);
    expect(names).not.toContain("undoRedo");
    expect(names).not.toContain("history");
    expect(names).not.toContain("codeBlock");
    expect(names).not.toContain("horizontalRule");
  });

  it("superscript and subscript exclude each other", () => {
    const schema = getEditorSchema();
    const sup = schema.marks.superscript!.create();
    const sub = schema.marks.subscript!.create();
    expect(sup.addToSet([sub]).map((m) => m.type.name)).toEqual(["superscript"]);
  });

  it("an image renders from the room-scoped URL and never parses a remote or base64 src", () => {
    const schema = getEditorSchema({ assetUrl: (a) => `/rooms/r1/assets/${a}` });
    const image = schema.nodes.image!.create({ assetId: id(9), alt: "x", width: 10, height: 5 });
    const spec = schema.nodes.image!.spec.toDOM!(image) as unknown as [string, Record<string, string>];
    expect(spec[1].src).toBe(`/rooms/r1/assets/${id(9)}`);
    expect(JSON.stringify(image.toJSON())).not.toContain("src");
    const parse = schema.nodes.image!.spec.parseDOM!;
    expect(parse.every((rule) => rule.tag === "img[data-asset-id]")).toBe(true);
  });
});

const PINNED_FINGERPRINT = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "schema.fingerprint"), "utf-8").trim();
