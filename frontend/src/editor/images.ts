/**
 * Image commands (pure). An image is an inline ATOM: one offset unit, deleted and selected as a unit,
 * never split. It references an uploaded asset by id (the bytes live on the server, checked and
 * re-encoded there); the document only stores the reference, a display size and alt text.
 */
import type { ImageNode } from "threadforge-client/document-schema";
import { type Outcome, type Pos, type Sel, caret, deleteSelection, isCollapsed, ordered } from "./commands";
import { type Doc, type TextBlock, indexOf, newId, textBlock, updateNode, withInlines } from "./doc";
import { insertAtomInlines, inlineLen } from "./inline";

export const MAX_ALT = 500;
/** Inserted images are scaled down to fit the page body; the stored size is what they display at. */
export const MAX_INSERT_WIDTH_PX = 600;

export interface ImageInput {
  asset_id: string;
  width_px: number;
  height_px: number;
  alt?: string;
}

export function fitSize(width: number, height: number): { width_px: number; height_px: number } {
  if (width <= MAX_INSERT_WIDTH_PX) return { width_px: width, height_px: height };
  const scale = MAX_INSERT_WIDTH_PX / width;
  return { width_px: MAX_INSERT_WIDTH_PX, height_px: Math.max(1, Math.round(height * scale)) };
}

/** Where an image is: its text block and the offset of the atom. */
export function locateImage(doc: Doc, imageId: string): { blockId: string; offset: number; image: ImageNode } | null {
  for (const blockId of indexOf(doc).order) {
    let pos = 0;
    for (const c of textBlock(doc, blockId).children ?? []) {
      if (c.type === "image" && c.id === imageId) return { blockId, offset: pos, image: c };
      pos += inlineLen(c);
    }
  }
  return null;
}

export function insertImage(doc: Doc, sel: Sel, input: ImageInput): Outcome {
  let working = doc;
  let pos: Pos = sel.focus;
  if (!isCollapsed(sel)) {
    const r = deleteSelection(doc, sel);
    if ("blocked" in r) return r;
    working = r.doc;
    pos = r.pos;
  }
  const size = fitSize(input.width_px, input.height_px);
  const image: ImageNode = {
    type: "image",
    id: newId(),
    asset_id: input.asset_id,
    ...size,
    ...(input.alt ? { alt: input.alt.slice(0, MAX_ALT) } : {}),
  };
  const next = updateNode(working, pos.id, (n) =>
    withInlines(n as TextBlock, insertAtomInlines((n as TextBlock).children, pos.offset, image)),
  );
  return { doc: next, sel: caret(pos.id, pos.offset + 1) };
}

/** The image the selection covers exactly (and nothing else), if any. */
export function selectedImage(doc: Doc, sel: Sel): ImageNode | null {
  const { start, end } = ordered(doc, sel);
  if (start.id !== end.id || end.offset - start.offset !== 1) return null;
  let pos = 0;
  for (const c of textBlock(doc, start.id).children ?? []) {
    const len = inlineLen(c);
    if (pos === start.offset) return c.type === "image" ? c : null;
    pos += len;
    if (pos > start.offset) return null;
  }
  return null;
}

/** A selection that covers exactly this image. */
export function imageSelection(doc: Doc, imageId: string): Sel | null {
  const at = locateImage(doc, imageId);
  return at ? { anchor: { id: at.blockId, offset: at.offset }, focus: { id: at.blockId, offset: at.offset + 1 } } : null;
}

/** Alt text: plain text up to 500 characters; line breaks and control characters become spaces. */
export function cleanAlt(raw: string): string {
  let out = "";
  for (const ch of raw) {
    const o = ch.codePointAt(0)!;
    out += o < 0x20 || (o >= 0x7f && o <= 0x9f) || o === 0x2028 || o === 0x2029 || (o >= 0xd800 && o <= 0xdfff) ? " " : ch;
  }
  return Array.from(out).slice(0, MAX_ALT).join("").trim();
}

export function setImageAlt(doc: Doc, imageId: string, alt: string): Outcome {
  const at = locateImage(doc, imageId);
  if (!at) return { blocked: "That image is no longer in the document." };
  const clean = cleanAlt(alt);
  if ((at.image.alt ?? "") === clean) return null;
  const next = updateNode(doc, at.blockId, (n) => {
    const tb = n as TextBlock;
    return withInlines(
      tb,
      (tb.children ?? []).map((c) => {
        if (c.type !== "image" || c.id !== imageId) return c;
        const { alt: _old, ...rest } = c;
        return clean ? { ...rest, alt: clean } : rest;
      }),
    );
  });
  return { doc: next, sel: imageSelection(next, imageId) ?? caret(at.blockId, at.offset) };
}
