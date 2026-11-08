/**
 * Operations on a text block's inline sequence.
 *
 * OFFSETS: a position inside a text block counts UTF-16 code units of text plus 1 for every inline
 * atom (hard break, image). `flatten()` produces a string in which each atom is U+FFFC, so string
 * offsets and model offsets are the same numbers, and Intl.Segmenter can be run on it directly.
 *
 * Text is never split inside a surrogate pair or a grapheme cluster by the commands (they move by
 * segmenter boundaries); these functions split wherever they are told and keep marks intact.
 */
import { canonicalJson, type Inline, type Json, type Marks, type TextNode } from "threadforge-client/document-schema";

export const ATOM = "￼";

export function inlineLen(n: Inline): number {
  return n.type === "text" ? n.text.length : 1;
}

export function flatten(children: readonly Inline[] | undefined): string {
  if (!children) return "";
  let s = "";
  for (const c of children) s += c.type === "text" ? c.text : ATOM;
  return s;
}

const markKey = (m: Marks | undefined): string => (m ? canonicalJson(m as unknown as Json) : "{}");
export const sameMarks = (a: Marks | undefined, b: Marks | undefined): boolean => markKey(a) === markKey(b);

/** Merge adjacent text nodes with identical marks and drop empty ones (the canonical rules). */
export function normalizeInlines(children: readonly Inline[]): Inline[] {
  const out: Inline[] = [];
  for (const c of children) {
    if (c.type === "text") {
      if (c.text === "") continue;
      const last = out[out.length - 1];
      if (last && last.type === "text" && sameMarks(last.marks, c.marks)) {
        out[out.length - 1] = { ...last, text: last.text + c.text };
        continue;
      }
    }
    out.push(c);
  }
  return out;
}

/** The sub-sequence [from, to) with marks kept; atoms count as one unit each. */
export function sliceInlines(children: readonly Inline[] | undefined, from: number, to: number): Inline[] {
  const out: Inline[] = [];
  if (!children || to <= from) return out;
  let pos = 0;
  for (const c of children) {
    const len = inlineLen(c);
    const start = pos;
    const end = pos + len;
    pos = end;
    if (end <= from) continue;
    if (start >= to) break;
    if (c.type === "text") {
      const a = Math.max(from, start) - start;
      const b = Math.min(to, end) - start;
      if (b > a) out.push(a === 0 && b === len ? c : ({ ...c, text: c.text.slice(a, b) } as TextNode));
    } else out.push(c); // an atom is wholly inside or outside
  }
  return out;
}

export function deleteInlines(children: readonly Inline[] | undefined, from: number, to: number): Inline[] {
  const len = inlineLen2(children);
  return normalizeInlines([...sliceInlines(children, 0, from), ...sliceInlines(children, to, len)]);
}

export const inlineLen2 = (children: readonly Inline[] | undefined): number =>
  children ? children.reduce((n, c) => n + inlineLen(c), 0) : 0;

/**
 * The marks new text at `offset` inherits: those of the character before it, or (at the start of a
 * block, or right after an atom) those of the character after it, else none. Typing-state marks at a
 * caret (a bold toggle in an empty place) override this in the editor and are not stored.
 */
export function inheritMarks(children: readonly Inline[] | undefined, offset: number): Marks | undefined {
  if (!children) return undefined;
  let pos = 0;
  let nextText: TextNode | undefined; // the first text node starting at or after the offset
  let prevText: TextNode | undefined; // the text node containing the character just before it
  for (const c of children) {
    const len = inlineLen(c);
    if (c.type === "text") {
      if (pos < offset && offset <= pos + len) prevText = c;
      if (!nextText && pos >= offset) nextText = c;
    }
    pos += len;
  }
  return prevText ? prevText.marks : nextText?.marks;
}

export function insertTextInlines(
  children: readonly Inline[] | undefined,
  offset: number,
  text: string,
  marks: Marks | undefined,
): Inline[] {
  const node: TextNode = marks && Object.keys(marks).length > 0 ? { type: "text", text, marks } : { type: "text", text };
  const len = inlineLen2(children);
  return normalizeInlines([...sliceInlines(children, 0, offset), node, ...sliceInlines(children, offset, len)]);
}

export function insertAtomInlines(children: readonly Inline[] | undefined, offset: number, atom: Inline): Inline[] {
  const len = inlineLen2(children);
  return normalizeInlines([...sliceInlines(children, 0, offset), atom, ...sliceInlines(children, offset, len)]);
}
