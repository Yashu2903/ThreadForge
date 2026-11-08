/**
 * The browser side imports the SAME schema package as the collaboration service will. This test pins
 * the schema fingerprint (packages/editor-schema/schema.fingerprint); the collab service's test
 * pins the same file. If either side ever builds a different schema, one of them fails.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { adapt, createExtensions, getEditorSchema, schemaFingerprint, validateDocument } from "@threadforge/editor-schema";

const pinned = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "..", "packages", "editor-schema", "schema.fingerprint"), "utf-8").trim();

describe("@threadforge/editor-schema in the browser workspace", () => {
  it("builds the same schema the package pins", async () => {
    expect(await schemaFingerprint()).toBe(pinned);
  });

  it("adapts and validates an interchange document", () => {
    const doc = adapt({ schema_version: 1, blocks: [{ type: "paragraph", id: "00000000-0000-4000-8000-000000000001", children: [{ type: "text", text: "hi" }] }] });
    expect(validateDocument(doc).ok).toBe(true);
    expect(getEditorSchema().nodeFromJSON(doc).textContent).toBe("hi");
  });

  it("the browser editor and the validator use one extension list", () => {
    expect(createExtensions().map((e) => e.name)).toContain("paragraphFormat");
  });
});
