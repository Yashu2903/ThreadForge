/**
 * The `diagram` node: schema shape, validation (strict and structure-only), hashing, text, adapter and
 * the Yjs round trip. The fixtures in test/fixtures/diagrams.json are plain Tiptap JSON shared with the
 * collaboration service's and the frontend's tests.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { prosemirrorJSONToYDoc, yDocToProsemirrorJSON } from "@tiptap/y-tiptap";
import { describe, expect, it } from "vitest";
import * as Y from "yjs";
import { DEFAULT_LIMITS, type PMJson, YJS_FIELD, adapt, blockHash, canonicalize, extractText, getEditorSchema, validateDocument } from "../src";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const FLOW = "flowchart TD\n  A[Start] --> B[End]";
const diagram = (n: number, source: string, extra: object = {}): PMJson => ({
  type: "diagram",
  attrs: { id: id(n), ...extra },
  ...(source ? { content: [{ type: "text", text: source }] } : {}),
});
const p = (n: number, text: string): PMJson => ({ type: "paragraph", attrs: { id: id(n) }, content: [{ type: "text", text }] });
const doc = (...content: PMJson[]): PMJson => ({ type: "doc", content });

const fixtures: { name: string; ok: boolean; policy?: "strict" | "structure"; code?: string; doc: PMJson }[] = JSON.parse(
  readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", "diagrams.json"), "utf-8"),
);

describe("diagram fixtures", () => {
  for (const f of fixtures) {
    it(f.name, () => {
      const r = validateDocument(f.doc, DEFAULT_LIMITS, getEditorSchema(), f.policy ?? "strict");
      expect(r.ok, JSON.stringify(r)).toBe(f.ok);
      if (!r.ok && f.code) expect(r.errors.map((e) => e.code)).toContain(f.code);
    });
  }
});

describe("the diagram node", () => {
  it("is a top-level block whose text content is the source, with no marks", () => {
    const type = getEditorSchema().nodes.diagram!;
    expect(type.spec.content).toBe("text*");
    expect(type.spec.marks).toBe("");
    expect(type.spec.code).toBe(true);
    expect(Object.keys(type.spec.attrs ?? {}).sort()).toEqual(["id", "language"]);
  });

  it("keeps line feeds in the source and nowhere else", () => {
    expect(validateDocument(doc(diagram(1, FLOW))).ok).toBe(true);
    const bad = validateDocument(doc({ type: "paragraph", attrs: { id: id(2) }, content: [{ type: "text", text: "a\nb" }] }));
    expect(bad.ok).toBe(false);
    const cr = validateDocument(doc(diagram(3, "flowchart TD\r\n  A --> B")));
    expect(cr.ok).toBe(false);
  });

  it("canonical form drops the default language and survives a ProseMirror round trip", () => {
    const r = validateDocument(doc(diagram(1, FLOW, { language: "mermaid" })));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.doc.content![0]!.attrs).toEqual({ id: id(1) });
    const back = canonicalize(getEditorSchema().nodeFromJSON(r.doc).toJSON() as PMJson);
    expect(back).toEqual(r.doc);
    expect(r.stats.diagrams).toBe(1);
  });

  it("rejects hostile or malformed sources in strict mode with a diagram_ code and a path", () => {
    const cases: [string, string][] = [
      ['click A href "https://evil.test"', "diagram_type_not_allowed"],
      ["flowchart TD\n  click A callback", "diagram_interaction_not_allowed"],
      ["flowchart TD\n  A[<img src=x onerror=alert(1)>] --> B", "diagram_html_not_allowed"],
      ['%%{init: {"securityLevel":"loose"}}%%\nflowchart TD\n  A-->B', "diagram_directive_not_allowed"],
      ['pie\n  "a": 1', "diagram_type_not_allowed"],
      ["flowchart TD\n  A[javascript:alert(1)] --> B", "diagram_scheme_not_allowed"],
      ["", "diagram_empty"],
      [`flowchart TD\n${"  A --> B\n".repeat(600)}`, "diagram_too_many_lines"],
    ];
    for (const [source, code] of cases) {
      const r = validateDocument(doc(diagram(1, source)));
      expect(r.ok, source).toBe(false);
      if (!r.ok) {
        expect(r.errors[0]!.code, source).toBe(code);
        expect(r.errors[0]!.path).toBe("/content/0/content");
      }
    }
  });

  it("structure mode lets half-typed sources through but still bounds size, location and language", () => {
    const half = (s: string) => validateDocument(doc(diagram(1, s)), DEFAULT_LIMITS, getEditorSchema(), "structure");
    expect(half("fl").ok).toBe(true);
    expect(half("flowchart TD\n  A[x < y] --> B").ok).toBe(true);
    expect(half("x".repeat(20_001)).ok).toBe(false);
    expect(validateDocument(doc(diagram(1, FLOW, { language: "dot" })), DEFAULT_LIMITS, getEditorSchema(), "structure").ok).toBe(false);
    const nested = { type: "blockquote", attrs: { id: id(5) }, content: [diagram(6, FLOW)] };
    expect(validateDocument(doc(nested), DEFAULT_LIMITS, getEditorSchema(), "structure").ok).toBe(false);
  });

  it("counts diagrams against maxDiagrams", () => {
    const many = doc(...Array.from({ length: 3 }, (_, i) => diagram(i + 1, FLOW)));
    const r = validateDocument(many, { ...DEFAULT_LIMITS, maxDiagrams: 2 });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.errors[0]).toMatchObject({ code: "limit_exceeded", path: "/content/2" });
  });

  it("its block hash covers the source and ignores the id; a one-character edit changes it", async () => {
    const a = await blockHash(diagram(1, FLOW));
    expect(a).toBe(await blockHash(diagram(2, FLOW)));
    expect(a).not.toBe(await blockHash(diagram(1, FLOW.replace("End", "Fin"))));
  });

  it("extractText gives the source", () => {
    expect(extractText(doc(p(1, "Before"), diagram(2, FLOW)))).toBe(`Before\n${FLOW}`);
  });

  it("the adapter maps an interchange-style diagram block and fails loudly on unknown keys", () => {
    const out = adapt({ schema_version: 1, blocks: [{ type: "diagram", id: id(1), source: FLOW }] });
    expect(out.content![0]).toEqual({ type: "diagram", attrs: { id: id(1) }, content: [{ type: "text", text: FLOW }] });
    expect(() => adapt({ schema_version: 1, blocks: [{ type: "diagram", id: id(1), source: FLOW, href: "x" }] })).toThrow(/unknown field/);
  });

  it("survives Yjs: seeded, read back, and two concurrent source edits both survive (character-level merge)", () => {
    const seeded = validateDocument(doc(diagram(1, "flowchart TD\n  A --> B")));
    expect(seeded.ok).toBe(true);
    if (!seeded.ok) return;
    const a = prosemirrorJSONToYDoc(getEditorSchema(), seeded.doc, YJS_FIELD);
    const b = new Y.Doc();
    Y.applyUpdate(b, Y.encodeStateAsUpdate(a));
    const textOf = (d: Y.Doc) => (d.getXmlFragment(YJS_FIELD).get(0) as Y.XmlElement).get(0) as Y.XmlText;
    textOf(a).insert(textOf(a).length, "\n  B --> C");
    textOf(b).insert(textOf(b).length, "\n  B --> D");
    Y.applyUpdate(a, Y.encodeStateAsUpdate(b));
    Y.applyUpdate(b, Y.encodeStateAsUpdate(a));
    const jsonA = yDocToProsemirrorJSON(a, YJS_FIELD) as PMJson;
    expect(jsonA).toEqual(yDocToProsemirrorJSON(b, YJS_FIELD));
    const merged = extractText(jsonA);
    expect(merged).toContain("B --> C");
    expect(merged).toContain("B --> D");
  });
});
