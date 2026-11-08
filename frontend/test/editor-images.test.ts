import { describe, expect, it } from "vitest";
import { validateDocument } from "threadforge-client/document-schema";
import { type Outcome, type Sel, caret, deleteBackward, deleteForward } from "../src/editor/commands";
import type { Doc } from "../src/editor/doc";
import { textBlock } from "../src/editor/doc";
import { MAX_INSERT_WIDTH_PX, cleanAlt, fitSize, imageSelection, insertImage, locateImage, selectedImage, setImageAlt } from "../src/editor/images";
import { flatten } from "../src/editor/inline";
import { docStats, limitProblem } from "../src/editor/stats";
import { DEFAULT_LIMITS } from "threadforge-client/document-schema";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const t = (text: string) => ({ type: "text" as const, text });
const p = (n: number, ...children: object[]) => ({ type: "paragraph" as const, id: id(n), ...(children.length ? { children } : {}) });
const doc = (...blocks: object[]): Doc => ({ schema_version: 1, blocks: blocks as never });
const ASSET = id(800);
const ok = (r: Outcome): { doc: Doc; sel: Sel } => {
  expect(r).not.toBeNull();
  expect(r).not.toHaveProperty("blocked");
  const out = r as { doc: Doc; sel: Sel };
  expect(() => validateDocument(out.doc)).not.toThrow();
  return out;
};

describe("image commands", () => {
  it("inserts an inline atom at the caret and moves the caret past it", () => {
    const r = ok(insertImage(doc(p(1, t("ab"))), caret(id(1), 1), { asset_id: ASSET, width_px: 80, height_px: 60 }));
    const kids = textBlock(r.doc, id(1)).children!;
    expect(kids.map((c) => c.type)).toEqual(["text", "image", "text"]);
    expect(kids[1]).toMatchObject({ asset_id: ASSET, width_px: 80, height_px: 60 });
    expect(kids[1]).not.toHaveProperty("alt"); // alt text starts empty
    expect(r.sel).toEqual(caret(id(1), 2));
  });

  it("replaces a selection", () => {
    const r = ok(insertImage(doc(p(1, t("abcd"))), { anchor: { id: id(1), offset: 1 }, focus: { id: id(1), offset: 3 } }, { asset_id: ASSET, width_px: 5, height_px: 5 }));
    expect(textBlock(r.doc, id(1)).children!.map((c) => c.type)).toEqual(["text", "image", "text"]);
    expect(flatten(textBlock(r.doc, id(1)).children)).toBe("a￼d");
  });

  it("scales large images down to the page width, keeping the aspect ratio", () => {
    expect(fitSize(1200, 600)).toEqual({ width_px: MAX_INSERT_WIDTH_PX, height_px: 300 });
    expect(fitSize(300, 200)).toEqual({ width_px: 300, height_px: 200 });
    expect(fitSize(5000, 1)).toEqual({ width_px: MAX_INSERT_WIDTH_PX, height_px: 1 });
  });

  it("each inserted image gets its own id, even for the same asset", () => {
    let d = doc(p(1));
    d = ok(insertImage(d, caret(id(1), 0), { asset_id: ASSET, width_px: 5, height_px: 5 })).doc;
    d = ok(insertImage(d, caret(id(1), 1), { asset_id: ASSET, width_px: 5, height_px: 5 })).doc;
    const ids = textBlock(d, id(1)).children!.map((c) => (c as { id: string }).id);
    expect(new Set(ids).size).toBe(2);
  });

  it("selects exactly one image as a unit and deleting removes it whole", () => {
    const d = ok(insertImage(doc(p(1, t("ab"))), caret(id(1), 1), { asset_id: ASSET, width_px: 5, height_px: 5 })).doc;
    const image = textBlock(d, id(1)).children![1] as { id: string };
    const sel = imageSelection(d, image.id)!;
    expect(sel).toEqual({ anchor: { id: id(1), offset: 1 }, focus: { id: id(1), offset: 2 } });
    expect(selectedImage(d, sel)?.id).toBe(image.id);
    expect(selectedImage(d, caret(id(1), 1))).toBeNull();
    expect(selectedImage(d, { anchor: { id: id(1), offset: 0 }, focus: { id: id(1), offset: 2 } })).toBeNull(); // text + image
    expect(textBlock(ok(deleteBackward(d, caret(id(1), 2))).doc, id(1)).children).toEqual([t("ab")]);
    expect(textBlock(ok(deleteForward(d, caret(id(1), 1))).doc, id(1)).children).toEqual([t("ab")]);
    expect(locateImage(d, image.id)).toMatchObject({ blockId: id(1), offset: 1 });
  });

  it("sets and clears alt text; control characters become spaces; it is capped at 500 characters", () => {
    const d = ok(insertImage(doc(p(1)), caret(id(1), 0), { asset_id: ASSET, width_px: 5, height_px: 5 })).doc;
    const imageId = (textBlock(d, id(1)).children![0] as { id: string }).id;
    const withAlt = ok(setImageAlt(d, imageId, "  A chart\nof sales\u0000  "));
    expect((textBlock(withAlt.doc, id(1)).children![0] as { alt: string }).alt).toBe("A chart of sales");
    expect(withAlt.sel).toEqual(imageSelection(withAlt.doc, imageId));
    expect(setImageAlt(withAlt.doc, imageId, "A chart of sales")).toBeNull(); // unchanged
    const cleared = ok(setImageAlt(withAlt.doc, imageId, "   "));
    expect(textBlock(cleared.doc, id(1)).children![0]).not.toHaveProperty("alt");
    expect(Array.from(cleanAlt("é".repeat(600))).length).toBe(500);
    expect(setImageAlt(d, "missing", "x")).toHaveProperty("blocked");
  });

  it("images count against the per-document limit", () => {
    const limits = { ...DEFAULT_LIMITS, maxImages: 1 };
    const one = ok(insertImage(doc(p(1)), caret(id(1), 0), { asset_id: ASSET, width_px: 5, height_px: 5 })).doc;
    const two = ok(insertImage(one, caret(id(1), 1), { asset_id: ASSET, width_px: 5, height_px: 5 })).doc;
    expect(docStats(two).images).toBe(2);
    expect(limitProblem(one, two, limits)).toMatch(/at most 1 images/);
    expect(limitProblem(doc(p(1)), one, limits)).toBeNull();
  });
});
