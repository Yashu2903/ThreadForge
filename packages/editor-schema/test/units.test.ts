/** UTF-16 range mapping, against the cases SHARED with the Python implementation
 *  (backend/tests/test_edit_units.py reads the same file). */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { type PMJson, RangeError_, blockUnits, textHash, utf16Slice } from "../src";

const cases: { name: string; block: PMJson; units: string; slices: [number, number, string | null][] }[] = JSON.parse(
  readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", "range-cases.json"), "utf-8"),
);

describe("block units and ranges", () => {
  for (const c of cases) {
    it(c.name, () => {
      const units = blockUnits(c.block);
      expect(units).toBe(c.units);
      for (const [start, end, expected] of c.slices) {
        if (expected === null) expect(() => utf16Slice(units, start, end), `${start}..${end}`).toThrow(RangeError_);
        else expect(utf16Slice(units, start, end), `${start}..${end}`).toBe(expected);
      }
    });
  }

  it("refuses containers", () => {
    expect(() => blockUnits({ type: "bulletList", attrs: { id: "x" } })).toThrow(RangeError_);
  });

  it("textHash is SHA-256 of the UTF-8 bytes", async () => {
    for (const text of ["", "abc", "a😀b", "é"]) {
      expect(await textHash(text)).toBe(createHash("sha256").update(text, "utf-8").digest("hex"));
    }
  });
});
