/**
 * Formatting commands (pure, like commands.ts): marks, paragraph attributes, block kinds, links, and
 * the queries the toolbar needs (tri-state values for a mixed selection).
 *
 * A selection can span several text blocks (and, for these commands, several table cells: marks and
 * alignment are allowed across cells, unlike typing and deleting). `blockRanges` slices the
 * selection into one [from, to) range per text block, in document order.
 *
 * Marks at a COLLAPSED caret are not stored in the document (an empty run cannot carry marks): the
 * editor keeps them as "typing state" and applies them to the next text typed. Formatting of an empty
 * paragraph is therefore not persisted until text is typed into it.
 */
import type { Heading, Inline, Marks, Paragraph, ParagraphAttrs } from "threadforge-client/document-schema";
import { canonicalJson, checkLink, DEFAULT_LIMITS, type Json } from "threadforge-client/document-schema";
import { type Outcome, type Sel, caret, isCollapsed, ordered } from "./commands";
import { type Doc, type TextBlock, blockLength, indexOf, parentOf, textBlock, updateNode, withInlines } from "./doc";
import { inheritMarks, inlineLen, normalizeInlines, sliceInlines } from "./inline";

export interface BlockRange {
  id: string;
  from: number;
  to: number;
}

export function blockRanges(doc: Doc, sel: Sel): BlockRange[] {
  const { start, end } = ordered(doc, sel);
  const idx = indexOf(doc);
  const out: BlockRange[] = [];
  for (let i = idx.orderIdx.get(start.id)!; i <= idx.orderIdx.get(end.id)!; i++) {
    const id = idx.order[i]!;
    out.push({ id, from: id === start.id ? start.offset : 0, to: id === end.id ? end.offset : blockLength(textBlock(doc, id)) });
  }
  return out;
}

// ---- marks -------------------------------------------------------------------------------------------------------

export type BoolMark = "bold" | "italic" | "underline" | "strike";
export type ValueMark = "font" | "size_pt" | "color" | "highlight" | "script";
export type Tri = "on" | "off" | "mixed";

const cleanMarks = (m: Marks): Marks | undefined => {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(m)) if (v !== undefined && v !== null && v !== false) out[k] = v;
  return Object.keys(out).length ? (out as Marks) : undefined;
};

/** Apply `fn` to the marks of every text node in [from, to) of a block's inlines, splitting at the ends. */
function mapMarks(children: readonly Inline[] | undefined, from: number, to: number, fn: (m: Marks) => Marks): Inline[] {
  const len = blockLenOf(children);
  const middle = sliceInlines(children, from, to).map((n): Inline => {
    if (n.type !== "text") return n;
    const marks = cleanMarks(fn({ ...(n.marks ?? {}) }));
    const { marks: _drop, ...rest } = n;
    return marks ? { ...rest, marks } : rest;
  });
  return normalizeInlines([...sliceInlines(children, 0, from), ...middle, ...sliceInlines(children, to, len)]);
}
const blockLenOf = (children: readonly Inline[] | undefined): number => (children ?? []).reduce((n, c) => n + inlineLen(c), 0);

function applyToRanges(doc: Doc, sel: Sel, fn: (m: Marks) => Marks): Doc {
  let next = doc;
  for (const r of blockRanges(doc, sel)) {
    if (r.to <= r.from) continue;
    next = updateNode(next, r.id, (n) => withInlines(n as TextBlock, mapMarks((n as TextBlock).children, r.from, r.to, fn)));
  }
  return next;
}

/** The marks of every text node touched by the selection (the node is "touched" if any of its characters are in range). */
function touchedMarks(doc: Doc, sel: Sel): Marks[] {
  const out: Marks[] = [];
  for (const r of blockRanges(doc, sel)) {
    for (const n of sliceInlines(textBlock(doc, r.id).children, r.from, r.to)) if (n.type === "text") out.push(n.marks ?? {});
  }
  return out;
}

export function boolMarkState(doc: Doc, sel: Sel, key: BoolMark, typing?: Marks | null): Tri {
  if (isCollapsed(sel)) {
    const m = typing ?? inheritMarks(textBlock(doc, sel.focus.id).children, sel.focus.offset);
    return m?.[key] ? "on" : "off";
  }
  const all = touchedMarks(doc, sel);
  if (all.length === 0) return "off";
  const on = all.filter((m) => m[key]).length;
  return on === all.length ? "on" : on === 0 ? "off" : "mixed";
}

/** The shared value of a mark over the selection, `null` if none has it, or "mixed". */
export function valueMarkState(doc: Doc, sel: Sel, key: ValueMark | "link", typing?: Marks | null): Json | "mixed" {
  const sets = isCollapsed(sel)
    ? [typing ?? inheritMarks(textBlock(doc, sel.focus.id).children, sel.focus.offset) ?? {}]
    : touchedMarks(doc, sel);
  if (sets.length === 0) return null;
  const values = new Set(sets.map((m) => canonicalJson(((m as Record<string, unknown>)[key] ?? null) as Json)));
  if (values.size > 1) return "mixed";
  return JSON.parse([...values][0]!) as Json;
}

/** Toggle bold/italic/underline/strike. A mixed selection turns the mark ON everywhere. */
export function toggleMark(doc: Doc, sel: Sel, key: BoolMark): Outcome {
  if (isCollapsed(sel)) return null;
  const turnOn = boolMarkState(doc, sel, key) !== "on";
  const next = applyToRanges(doc, sel, (m) => ({ ...m, [key]: turnOn ? true : undefined }));
  return next === doc ? null : { doc: next, sel };
}

/** Set (or, with null, clear) font, size, colour, highlight or script over the selection. */
export function setMark(doc: Doc, sel: Sel, key: ValueMark, value: string | number | null): Outcome {
  if (isCollapsed(sel)) return null;
  const problem = markValueProblem(key, value);
  if (problem) return { blocked: problem };
  const next = applyToRanges(doc, sel, (m) => {
    const copy: Record<string, unknown> = { ...m };
    if (value === null) delete copy[key];
    else copy[key] = key === "color" || key === "highlight" ? String(value).toUpperCase() : value;
    return copy as Marks;
  });
  return next === doc ? null : { doc: next, sel };
}

export function markValueProblem(key: ValueMark, value: string | number | null, fonts: readonly string[] = DEFAULT_LIMITS.allowedFonts): string | null {
  if (value === null) return null;
  if (key === "size_pt" && !(typeof value === "number" && value >= 8 && value <= 72 && Number.isInteger(value * 2))) return "Font size must be 8 to 72 in steps of 0.5.";
  if ((key === "color" || key === "highlight") && !(typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value))) return "Colours look like #RRGGBB.";
  if (key === "font" && !(typeof value === "string" && fonts.includes(value))) return "That font is not available.";
  if (key === "script" && value !== "superscript" && value !== "subscript") return "Script must be superscript or subscript.";
  return null;
}

// ---- links ---------------------------------------------------------------------------------------------------

export interface LinkInfo {
  href: string;
  /** the whole run of text carrying this link around the caret/selection */
  id: string;
  from: number;
  to: number;
}

/** The link under a collapsed caret (before or after it), or covering the whole selection. */
export function linkAt(doc: Doc, sel: Sel): LinkInfo | null {
  const { start, end } = ordered(doc, sel);
  if (start.id !== end.id) return null;
  const children = textBlock(doc, start.id).children ?? [];
  const nodes: { pos: number; len: number; href: string | null }[] = [];
  let pos = 0;
  for (const c of children) {
    const len = inlineLen(c);
    nodes.push({ pos, len, href: c.type === "text" ? (c.marks?.link?.href ?? null) : null });
    pos += len;
  }
  let hit = -1;
  if (start.offset === end.offset) {
    const o = start.offset;
    hit = nodes.findIndex((n) => n.href && n.pos < o && o <= n.pos + n.len); // the character before the caret
    if (hit < 0) hit = nodes.findIndex((n) => n.href && n.pos <= o && o < n.pos + n.len); // or the one after it
  } else {
    const touched = nodes.map((n, i) => ({ n, i })).filter(({ n }) => n.pos < end.offset && n.pos + n.len > start.offset);
    const hrefs = new Set(touched.map(({ n }) => n.href));
    if (touched.length > 0 && hrefs.size === 1 && touched[0]!.n.href) hit = touched[0]!.i;
  }
  if (hit < 0) return null;
  const href = nodes[hit]!.href!;
  let a = hit;
  let b = hit;
  while (a > 0 && nodes[a - 1]!.href === href) a--;
  while (b < nodes.length - 1 && nodes[b + 1]!.href === href) b++;
  return { href, id: start.id, from: nodes[a]!.pos, to: nodes[b]!.pos + nodes[b]!.len };
}

/** Make the selection (or, at a caret inside a link, the whole link) point at `href`. */
export function setLink(doc: Doc, sel: Sel, href: string): Outcome {
  const clean = checkLink(href, DEFAULT_LIMITS.maxLinkLength);
  if (clean === null) return { blocked: "Links must start with http://, https:// or mailto:." };
  const existing = isCollapsed(sel) ? linkAt(doc, sel) : null;
  if (isCollapsed(sel) && !existing) return { blocked: "Select some text to make a link." };
  const target: Sel = existing ? { anchor: { id: existing.id, offset: existing.from }, focus: { id: existing.id, offset: existing.to } } : sel;
  const next = applyToRanges(doc, target, (m) => ({ ...m, link: { href: clean } }));
  return next === doc ? null : { doc: next, sel };
}

export function removeLink(doc: Doc, sel: Sel): Outcome {
  const existing = isCollapsed(sel) ? linkAt(doc, sel) : null;
  const target: Sel = existing ? { anchor: { id: existing.id, offset: existing.from }, focus: { id: existing.id, offset: existing.to } } : sel;
  if (isCollapsed(target)) return null;
  const next = applyToRanges(doc, target, (m) => ({ ...m, link: undefined }));
  return next === doc ? null : { doc: next, sel };
}

// ---- paragraph attributes ---------------------------------------------------------------------------------------------

export interface AttrPatch {
  align?: "left" | "center" | "right" | "justify";
  line_spacing?: number;
  space_before_pt?: number;
  space_after_pt?: number;
  indent_level?: number;
}

const DEFAULT_ATTRS: Record<string, unknown> = { align: "left", line_spacing: 1, space_before_pt: 0, space_after_pt: 0, indent_level: 0 };
const LINE_SPACINGS = [1, 1.15, 1.5, 2];

function patchProblem(patch: AttrPatch): string | null {
  if (patch.align !== undefined && !["left", "center", "right", "justify"].includes(patch.align)) return "Unknown alignment.";
  if (patch.line_spacing !== undefined && !LINE_SPACINGS.includes(patch.line_spacing)) return "Line spacing must be 1, 1.15, 1.5 or 2.";
  for (const k of ["space_before_pt", "space_after_pt"] as const) {
    const v = patch[k];
    if (v !== undefined && !(v >= 0 && v <= 72 && Number.isInteger(v * 2))) return "Spacing must be 0 to 72 points in steps of 0.5.";
  }
  if (patch.indent_level !== undefined && !(Number.isInteger(patch.indent_level) && patch.indent_level >= 0 && patch.indent_level <= 8)) return "Indent must be 0 to 8.";
  return null;
}

const inListItem = (doc: Doc, id: string): boolean => parentOf(doc, id)?.type === "list_item";

export function setAttrs(doc: Doc, sel: Sel, patch: AttrPatch): Outcome {
  const problem = patchProblem(patch);
  if (problem) return { blocked: problem };
  let next = doc;
  for (const r of blockRanges(doc, sel)) {
    next = updateNode(next, r.id, (n) => {
      const tb = n as TextBlock;
      const merged: Record<string, unknown> = { ...(tb.attrs ?? {}) };
      for (const [k, v] of Object.entries(patch)) {
        if (k === "indent_level" && inListItem(doc, tb.id)) continue; // list nesting does the indenting
        if (v === undefined || v === DEFAULT_ATTRS[k]) delete merged[k];
        else merged[k] = v;
      }
      const attrs = Object.keys(merged).length ? (merged as ParagraphAttrs) : undefined;
      if (JSON.stringify(attrs ?? null) === JSON.stringify(tb.attrs ?? null)) return tb;
      const { attrs: _old, ...rest } = tb;
      return (attrs ? { ...rest, attrs } : rest) as TextBlock;
    });
  }
  return next === doc ? null : { doc: next, sel };
}

/** Indent / outdent by whole levels (36 pt each), clamped to 0..8; list items are skipped. */
export function changeIndent(doc: Doc, sel: Sel, delta: number): Outcome {
  let next = doc;
  for (const r of blockRanges(doc, sel)) {
    if (inListItem(doc, r.id)) continue;
    const level = textBlock(doc, r.id).attrs?.indent_level ?? 0;
    const wanted = Math.max(0, Math.min(8, level + delta));
    if (wanted !== level) {
      const r2 = setAttrs(next, caret(r.id, 0), { indent_level: wanted });
      if (r2 && !("blocked" in r2)) next = r2.doc;
    }
  }
  return next === doc ? null : { doc: next, sel };
}

export type AttrKey = "align" | "line_spacing" | "space_before_pt" | "space_after_pt" | "indent_level";
export function attrState(doc: Doc, sel: Sel, key: AttrKey): unknown | "mixed" {
  const values = new Set(blockRanges(doc, sel).map((r) => JSON.stringify(textBlock(doc, r.id).attrs?.[key] ?? DEFAULT_ATTRS[key])));
  return values.size > 1 ? "mixed" : JSON.parse([...values][0]!);
}

// ---- block kind (paragraph / heading) ---------------------------------------------------------------------------------------

export type BlockKind = "paragraph" | "h1" | "h2" | "h3";

/** Headings exist only directly in the body: quotes, list items and cells hold paragraphs only. */
export function setBlockKind(doc: Doc, sel: Sel, kind: BlockKind): Outcome {
  let next = doc;
  let eligible = 0;
  for (const r of blockRanges(doc, sel)) {
    const tb = textBlock(doc, r.id);
    if (kind !== "paragraph" && parentOf(doc, r.id) !== null) continue;
    eligible++;
    next = updateNode(next, r.id, (n) => {
      const cur = n as TextBlock;
      if (kind === "paragraph") {
        if (cur.type === "paragraph") return cur;
        const { level: _level, ...rest } = cur as Heading;
        return { ...rest, type: "paragraph" } as Paragraph;
      }
      const level = Number(kind[1]) as 1 | 2 | 3;
      if (cur.type === "heading" && cur.level === level) return cur;
      return { ...cur, type: "heading", level } as Heading;
    });
    void tb;
  }
  if (eligible === 0) return { blocked: "Headings can only be used in the body of the document, not inside lists, quotes or tables." };
  return next === doc ? null : { doc: next, sel };
}

export function blockKindState(doc: Doc, sel: Sel): BlockKind | "mixed" {
  const kinds = new Set(
    blockRanges(doc, sel).map((r) => {
      const tb = textBlock(doc, r.id);
      return tb.type === "heading" ? (`h${tb.level}` as BlockKind) : "paragraph";
    }),
  );
  return kinds.size > 1 ? "mixed" : [...kinds][0]!;
}
