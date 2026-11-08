/**
 * Every CANONICAL interchange fixture (the adapter's input contract is validated, canonical interchange
 * JSON: the valid documents and the canonical pairs' outputs, shared with the Python validator) is
 * adapted to the editor schema and must (1) equal the expected Tiptap JSON written by an independent
 * Python port of the mapping table, (2) pass validateDocument, (3) survive a ProseMirror round trip
 * unchanged.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { type PMJson, adapt, canonicalStringify, canonicalize, getEditorSchema, validateDocument } from "../src";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "shared", "fixtures");
const read = (...p: string[]) => JSON.parse(readFileSync(join(FIXTURES, ...p), "utf-8"));

const cases: { name: string; interchange: unknown; expected: PMJson }[] = [];
for (const [kind, key] of [["valid", "document"], ["canonical", "output"]] as const) {
  for (const file of readdirSync(join(FIXTURES, kind)).filter((f) => f.endsWith(".json")).sort()) {
    const fixture = read(kind, file);
    cases.push({
      name: `${kind}/${file.replace(".json", "")}`,
      interchange: fixture[key],
      expected: read("tiptap", `${kind}-${file}`).expected,
    });
  }
}

describe("interchange fixtures", () => {
  it("covers every valid and canonical fixture", () => {
    expect(cases.length).toBe(13);
  });

  for (const c of cases) {
    it(`${c.name}: adapter output equals the independently written expectation`, () => {
      expect(adapt(c.interchange)).toEqual(c.expected);
    });

    it(`${c.name}: the adapted document is valid and survives a ProseMirror round trip`, () => {
      const adapted = adapt(c.interchange);
      const result = validateDocument(adapted);
      expect(result.ok, JSON.stringify(result)).toBe(true);
      const node = getEditorSchema().nodeFromJSON(adapted);
      node.check();
      expect(canonicalStringify(canonicalize(node.toJSON() as PMJson))).toBe(canonicalStringify(adapted));
    });
  }
});

describe("adapter failures are loud", () => {
  const base = { schema_version: 1, blocks: [{ type: "paragraph", id: "00000000-0000-4000-8000-000000000001" }] };
  it.each([
    ["not an object", [], ""],
    ["wrong version", { ...base, schema_version: 2 }, "/schema_version"],
    ["no blocks", { schema_version: 1, blocks: [] }, "/blocks"],
    ["unknown top-level key", { ...base, extra: 1 }, "/extra"],
    ["unknown block type", { schema_version: 1, blocks: [{ type: "div", id: "x" }] }, "/blocks/0/type"],
    ["unknown paragraph key", { schema_version: 1, blocks: [{ type: "paragraph", id: "x", style: "color:red" }] }, "/blocks/0/style"],
    ["unknown attr", { schema_version: 1, blocks: [{ type: "paragraph", id: "x", attrs: { color: "red" } }] }, "/blocks/0/attrs/color"],
    ["unknown inline type", { schema_version: 1, blocks: [{ type: "paragraph", id: "x", children: [{ type: "emoji" }] }] }, "/blocks/0/children/0/type"],
    ["unknown mark", { schema_version: 1, blocks: [{ type: "paragraph", id: "x", children: [{ type: "text", text: "a", marks: { blink: true } }] }] }, "/blocks/0/children/0/marks/blink"],
    ["missing id", { schema_version: 1, blocks: [{ type: "paragraph" }] }, "/blocks/0/id"],
    ["list item not an item", { schema_version: 1, blocks: [{ type: "list", id: "x", children: [{ type: "paragraph", id: "y" }] }] }, "/blocks/0/children/0"],
  ])("%s", (_name, input, path) => {
    try {
      adapt(input);
      throw new Error("should have thrown");
    } catch (err) {
      expect((err as { path?: string }).path).toBe(path);
    }
  });
});
