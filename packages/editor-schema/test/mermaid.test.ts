/**
 * The static Mermaid rules, against the cases SHARED with the Python implementation
 * (backend/tests/test_answer_mermaid.py reads the same file).
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { MERMAID_MAX_CHARS, MERMAID_MAX_LINES, MERMAID_TYPES, validateMermaidSource } from "../src";

const cases: { name: string; source: string; ok: boolean; code: string }[] = JSON.parse(
  readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", "mermaid-cases.json"), "utf-8"),
);

describe("validateMermaidSource", () => {
  for (const c of cases) {
    it(c.name, () => {
      const result = validateMermaidSource(c.source);
      expect(result.ok).toBe(c.ok);
      if (!result.ok) expect(result.code).toBe(c.code);
    });
  }

  it("enforces the size limits", () => {
    const tooLong = `flowchart TD\n  A --> B\n${"%% pad\n".repeat(5000)}`;
    expect(validateMermaidSource(tooLong)).toMatchObject({ ok: false, code: "too_long" });
    const tooManyLines = `flowchart TD\n${"%\n".repeat(MERMAID_MAX_LINES)}`;
    expect(tooManyLines.length).toBeLessThan(MERMAID_MAX_CHARS);
    expect(validateMermaidSource(tooManyLines)).toMatchObject({ ok: false, code: "too_many_lines" });
  });

  it("allows exactly the documented types", () => {
    expect([...MERMAID_TYPES].sort()).toEqual(
      ["classDiagram", "erDiagram", "flowchart", "gantt", "graph", "mindmap", "sequenceDiagram", "stateDiagram", "stateDiagram-v2", "timeline"].sort(),
    );
  });
});
