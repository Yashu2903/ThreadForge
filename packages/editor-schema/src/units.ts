/**
 * Positions inside a block's text, in UTF-16 code units. The TypeScript twin of
 * backend/app/ai/edit/units.py: both are tested against test/fixtures/range-cases.json, so the browser,
 * the collaboration service and the AI worker cannot disagree about what "characters 5 to 12" means.
 *
 * The text of a text-holding block (paragraph, heading, diagram) is its inline children in order:
 * text nodes contribute their characters (a character outside the Basic Multilingual Plane, an emoji for
 * instance, is TWO units); a hard break contributes one unit "\n"; an inline image contributes one unit
 * U+FFFC. Marks take no space. A boundary between the two halves of a surrogate pair is refused.
 */
import type { PMJson } from "./canonical.js";

export const OBJECT_REPLACEMENT = "￼";
export const TEXT_HOLDERS = ["paragraph", "heading", "diagram"] as const;

export class RangeError_ extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RangeError_";
  }
}

export const holdsText = (node: PMJson): boolean => (TEXT_HOLDERS as readonly string[]).includes(node.type ?? "");

export function blockUnits(node: PMJson): string {
  if (!holdsText(node)) throw new RangeError_("not a text block");
  let out = "";
  for (const child of node.content ?? []) {
    if (child.type === "text") out += child.text ?? "";
    else if (child.type === "hardBreak") out += "\n";
    else if (child.type === "image") out += OBJECT_REPLACEMENT;
    else throw new RangeError_(`unexpected inline node ${child.type}`);
  }
  return out;
}

/** text.slice(start, end) in UTF-16 units; RangeError_ outside the text or inside a surrogate pair. */
export function utf16Slice(text: string, start: number, end: number): string {
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end > text.length) {
    throw new RangeError_("range outside the text");
  }
  for (const boundary of [start, end]) {
    if (boundary > 0 && boundary < text.length) {
      const unit = text.charCodeAt(boundary);
      const before = text.charCodeAt(boundary - 1);
      if (unit >= 0xdc00 && unit <= 0xdfff && before >= 0xd800 && before <= 0xdbff) throw new RangeError_("range splits a surrogate pair");
    }
  }
  return text.slice(start, end);
}

/** SHA-256 (hex) of the UTF-8 bytes of the text: the hash a client sends for a range it selected. */
export async function textHash(text: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}
