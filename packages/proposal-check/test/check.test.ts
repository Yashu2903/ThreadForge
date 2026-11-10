import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { check } from "../src/check";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const doc = {
  type: "doc",
  content: [
    { type: "paragraph", attrs: { id: id(1) }, content: [{ type: "text", text: "Hello world" }] },
    { type: "paragraph", attrs: { id: id(2) }, content: [{ type: "text", text: "Second" }] },
  ],
};
const scope = { whole_ids: [id(1)], ranges: [], anchors: [id(1), id(2), "start"] };

describe("check", () => {
  it("builds the candidate of an edit and reports changes and hashes", async () => {
    const r = await check({ mode: "edit", content: doc, scope, operations: [{ op: "replace_block_text", block_id: id(1), new_inline: [{ text: "Hi" }] }] });
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.changes).toHaveLength(1);
    expect(r.hashes[id(1)]).toMatch(/^[0-9a-f]{64}$/);
  });

  it("refuses what is out of scope", async () => {
    const r = await check({ mode: "edit", content: doc, scope, operations: [{ op: "replace_block_text", block_id: id(2), new_inline: [{ text: "no" }] }] });
    expect(r).toMatchObject({ ok: false, errors: [{ code: "out_of_scope" }] });
  });

  it("builds a draft document", async () => {
    const r = await check({ mode: "draft", blocks: [{ type: "paragraph", inline: [{ text: "x" }] }] });
    expect(r.ok && r.doc?.content?.length).toBe(1);
  });

  it("refuses a document that is not valid or a request that is malformed", async () => {
    expect((await check({ mode: "edit", content: { type: "doc", content: [{ type: "div" }] } as never, scope, operations: [] })).ok).toBe(false);
    expect((await check({ mode: "edit", scope } as never)).ok).toBe(false);
  });
});

describe("the command line", () => {
  it("answers one JSON request on stdin with one JSON response", () => {
    const cli = join(dirname(fileURLToPath(import.meta.url)), "..", "dist", "cli.js");
    const out = execFileSync("node", [cli], { input: JSON.stringify({ mode: "draft", blocks: [{ type: "paragraph", inline: [{ text: "x" }] }] }), encoding: "utf-8" });
    expect(JSON.parse(out).ok).toBe(true);
  });
});
