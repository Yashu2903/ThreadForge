/** The TypeScript validator against the SAME golden fixtures as backend/tests/test_document_schema.py. */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  canonicalJson,
  checkLink,
  DEFAULT_FONT_KEYS,
  DEFAULT_LIMITS,
  InvalidDocument,
  titleError,
  validateDocument,
  type Json,
  type Limits,
} from "../src/document-schema.js";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "shared", "fixtures");

function load(folder: string): { name: string; [k: string]: any }[] {
  return readdirSync(join(FIXTURES, folder))
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((f) => JSON.parse(readFileSync(join(FIXTURES, folder, f), "utf-8")));
}

const LIMIT_KEYS: Record<string, keyof Limits> = {
  max_serialized_bytes: "maxSerializedBytes",
  max_text_codepoints: "maxTextCodepoints",
  max_total_nodes: "maxTotalNodes",
  max_blocks: "maxBlocks",
  max_inline_nodes: "maxInlineNodes",
  max_images: "maxImages",
  max_list_depth: "maxListDepth",
  max_link_length: "maxLinkLength",
};

function limitsFrom(fixture: { limits?: Record<string, number> }): Limits {
  const out: Limits = { ...DEFAULT_LIMITS };
  for (const [k, v] of Object.entries(fixture.limits ?? {})) (out as any)[LIMIT_KEYS[k]!] = v;
  return out;
}

describe("shared fixtures", () => {
  it("uses the same font allowlist as the server", () => {
    const config = JSON.parse(readFileSync(join(FIXTURES, "config.json"), "utf-8"));
    expect([...DEFAULT_FONT_KEYS].sort()).toEqual([...config.fonts].sort());
  });

  for (const fixture of load("valid")) {
    it(`valid: ${fixture.name}`, () => {
      const result = validateDocument(fixture.document);
      expect(result.content).toEqual(fixture.document);
      expect(canonicalJson(result.content as unknown as Json)).toBe(canonicalJson(fixture.document));
      expect(validateDocument(result.content).content).toEqual(result.content);
    });
  }

  for (const fixture of load("invalid")) {
    it(`invalid: ${fixture.name}`, () => {
      let caught: unknown;
      try {
        validateDocument(fixture.document, limitsFrom(fixture));
      } catch (err) {
        caught = err;
      }
      expect(caught).toBeInstanceOf(InvalidDocument);
      const e = caught as InvalidDocument;
      expect({ path: e.path, code: e.code }).toEqual(fixture.error);
    });
  }

  for (const fixture of load("canonical")) {
    it(`canonical: ${fixture.name}`, () => {
      const result = validateDocument(fixture.input);
      expect(result.content).toEqual(fixture.output);
      expect(canonicalJson(result.content as unknown as Json)).toBe(canonicalJson(fixture.output));
    });
  }

  it("has enough fixtures to mean something", () => {
    expect(load("valid").length).toBeGreaterThanOrEqual(8);
    expect(load("invalid").length).toBeGreaterThanOrEqual(50);
    expect(load("canonical").length).toBeGreaterThanOrEqual(5);
  });
});

describe("canonical JSON", () => {
  it("ignores key order and writes integral numbers as integers", () => {
    expect(canonicalJson({ b: 2, a: [1, { y: 1.5, x: true }] })).toBe('{"a":[1,{"x":true,"y":1.5}],"b":2}');
  });
  it("keeps non-ASCII text unescaped", () => {
    expect(canonicalJson({ t: "é\u2028😀" })).toBe('{"t":"é\u2028😀"}');
  });
});

describe("links", () => {
  it.each([
    ["https://example.com", "https://example.com"],
    ["HTTP://EXAMPLE.com/x", "http://EXAMPLE.com/x"],
    ["  mailto:a@b.co  ", "mailto:a@b.co"],
  ])("accepts %s", (href, expected) => expect(checkLink(href, 2048)).toBe(expected));

  it.each([
    "javascript:alert(1)",
    "jav\tascript:alert(1)",
    "java\u0000script:alert(1)",
    "data:text/html;base64,AAAA",
    "file:///c:/windows",
    "//example.com",
    "https://exa\u00a0mple.com",
    "https://example.com/\u2028x",
    "https://evil.com\\@good.com",
    "https://user@example.com",
    "",
    "mailto:",
  ])("refuses %j", (href) => expect(checkLink(href, 2048)).toBeNull());
});

describe("titles", () => {
  it.each(["Plan", "日本語のタイトル", "a".repeat(200), "emoji 😀", "zwj 👨‍👩‍👧"])("accepts %s", (t) =>
    expect(titleError(t)).toBeNull(),
  );
  it.each(["", " pad", "pad ", "a".repeat(201), "tab\there", "nul\u0000", "line\nbreak", "\u202eevil", "\ud800"])("refuses %j", (t) =>
    expect(titleError(t)).not.toBeNull(),
  );
});
