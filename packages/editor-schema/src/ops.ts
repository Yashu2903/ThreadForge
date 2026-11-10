/**
 * Edit operations on a document, as pure functions on ProseMirror JSON.
 *
 * ONE implementation, used twice:
 *   * the AI worker (through packages/proposal-check) builds and validates the CANDIDATE document of a
 *     proposal before any preview exists;
 *   * the collaboration service builds the same candidate from the LIVE document when a person applies
 *     the proposal, validates it, and only then mutates the shared Yjs document.
 * The model never emits Yjs, HTML or database writes: it emits these typed operations, and nothing else
 * can reach a document through this path.
 *
 * Operations (at most 50 per proposal, at most 30,000 inserted characters):
 *   replace_block_text   {block_id, new_inline[]}                    the whole inline content of one text block
 *   replace_range        {block_id, start, end, new_inline[]}        a range inside one text block (UTF-16 units)
 *   insert_blocks_after  {anchor_block_id | "start", blocks[]}       new TOP-LEVEL blocks after an anchor
 *   delete_blocks        {block_ids[]}
 * new_inline is a list of runs {text, marks[]}; marks are bold, italic, underline, strike and link (the
 * existing link policy). New blocks: paragraph, heading 1-3, quote, bullet and numbered lists (flat items
 * with a depth, at most the list nesting limit), tables (existing limits), diagram. No images, no fonts or
 * colours from the model.
 *
 * FORMATTING RULES. A replaced block keeps its element, its id and its block-level attributes (alignment,
 * spacing, indent). Replaced text inherits the textStyle (font, size, colour) and highlight marks of the
 * FIRST original run (of the block, or of the replaced range); the model's own marks are added to those.
 * New blocks use defaults.
 *
 * Every operation is checked against the SCOPE the request declared: replace/delete only inside the
 * selected blocks (replace_range inside a declared range), insertion only after a declared anchor. A
 * block can be the target of one operation only. The result is validated as a whole document.
 */
import { type MarkJson, type PMJson, canonicalize } from "./canonical.js";
import { DEFAULT_LIMITS, type Limits } from "./limits.js";
import { checkLink } from "./links.js";
import { validateMermaidSource } from "./mermaid.js";
import { RangeError_, blockUnits, holdsText, utf16Slice } from "./units.js";
import { validateDocument } from "./validate.js";

export const MAX_OPERATIONS = 50;
export const MAX_INSERTED_CHARS = 30_000;
export const START = "start";

export type RunMarkType = "bold" | "italic" | "underline" | "strike" | "link";
export interface RunMark {
  type: RunMarkType;
  href?: string | null;
}
export interface Run {
  text: string;
  marks?: RunMark[] | null;
}
export interface ListItemSpec {
  inline: Run[];
  depth: number;
}
export type NewBlock =
  | { type: "paragraph"; inline: Run[] }
  | { type: "heading"; level: number; inline: Run[] }
  | { type: "quote"; paragraphs: Run[][] }
  | { type: "bullet_list" | "numbered_list"; items: ListItemSpec[] }
  | { type: "table"; rows: Run[][][]; header_row?: boolean | null }
  | { type: "diagram"; mermaid: string };
export type Operation =
  | { op: "replace_block_text"; block_id: string; new_inline: Run[] }
  | { op: "replace_range"; block_id: string; start: number; end: number; new_inline: Run[] }
  | { op: "insert_blocks_after"; anchor_block_id: string; blocks: NewBlock[] }
  | { op: "delete_blocks"; block_ids: string[] };

/** What the request declared. `null` for a field means "not restricted" (used only by tests). */
export interface OpScope {
  /** blocks (and everything inside them) that may be replaced or deleted */
  wholeIds: ReadonlySet<string> | null;
  /** character ranges that replace_range may stay inside */
  ranges: readonly { block_id: string; start: number; end: number }[] | null;
  /** where new blocks may go: "start" and/or top-level block ids */
  anchors: readonly string[] | null;
}

export interface OpError {
  op: number | null;
  code: string;
  message: string;
  path?: string;
}
export interface Change {
  op: number;
  kind: Operation["op"];
  block_id: string | null;
  anchor: string | null;
  before: PMJson[];
  after: PMJson[];
}
export type OpResult =
  | { ok: true; doc: PMJson; changes: Change[]; insertedChars: number }
  | { ok: false; errors: OpError[] };

export interface ApplyOptions {
  scope?: OpScope;
  newId: () => string;
  limits?: Limits;
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const MARK_TYPES = new Set(["bold", "italic", "underline", "strike", "link"]);
const INHERITED = new Set(["textStyle", "highlight"]);

class Fail extends Error {
  constructor(
    readonly op: number | null,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

// ---- runs -> inline nodes ---------------------------------------------------------------------------------

function badRunText(text: string): string | null {
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c === 10 || c === 9) continue;
    if (c < 0x20 || (c >= 0x7f && c <= 0x9f) || c === 0x2028 || c === 0x2029) return "contains a control character";
    if (c >= 0xd800 && c <= 0xdbff) {
      const next = text.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) i++;
      else return "contains a lone surrogate";
    } else if (c >= 0xdc00 && c <= 0xdfff) return "contains a lone surrogate";
  }
  return null;
}

function markJson(op: number, raw: unknown, limits: Limits): MarkJson {
  if (!isObj(raw) || typeof raw.type !== "string" || !MARK_TYPES.has(raw.type)) throw new Fail(op, "invalid_mark", "marks are bold, italic, underline, strike and link");
  if (raw.type !== "link") return { type: raw.type };
  if (typeof raw.href !== "string") throw new Fail(op, "invalid_link", "a link needs an address");
  const href = checkLink(raw.href, limits.maxLinkLength);
  if (href === null) throw new Fail(op, "invalid_link", "only http, https and mailto links are allowed");
  return { type: "link", attrs: { href } };
}

/** Runs to inline nodes. A line feed inside a run becomes a hard break. Counts inserted characters. */
function inlineNodes(op: number, runs: unknown, inherited: MarkJson[], limits: Limits, counter: { chars: number }): PMJson[] {
  if (!Array.isArray(runs)) throw new Fail(op, "invalid_operation", "new_inline must be a list of runs");
  const out: PMJson[] = [];
  for (const run of runs) {
    if (!isObj(run) || typeof run.text !== "string") throw new Fail(op, "invalid_operation", "a run needs text");
    const bad = badRunText(run.text);
    if (bad) throw new Fail(op, "invalid_text", `text ${bad}`);
    const rawMarks = run.marks == null ? [] : run.marks;
    if (!Array.isArray(rawMarks)) throw new Fail(op, "invalid_mark", "marks must be a list");
    const marks = [...rawMarks.map((m) => markJson(op, m, limits)), ...inherited.map((m) => structuredClone(m))];
    const seen = new Set<string>();
    const unique = marks.filter((m) => (seen.has(m.type) ? false : (seen.add(m.type), true)));
    counter.chars += Array.from(run.text).length;
    run.text.split("\n").forEach((piece, i) => {
      if (i > 0) out.push({ type: "hardBreak" });
      if (piece) out.push(unique.length > 0 ? { type: "text", text: piece, marks: unique.map((m) => structuredClone(m)) } : { type: "text", text: piece });
    });
  }
  return out;
}

const inheritedMarksOf = (nodes: PMJson[] | undefined): MarkJson[] => {
  const first = (nodes ?? []).find((n) => n.type === "text");
  return (first?.marks ?? []).filter((m) => INHERITED.has(m.type)).map((m) => structuredClone(m));
};

// ---- new blocks -------------------------------------------------------------------------------------------

function paragraph(op: number, runs: unknown, newId: () => string, limits: Limits, counter: { chars: number }): PMJson {
  const content = inlineNodes(op, runs, [], limits, counter);
  return { type: "paragraph", attrs: { id: newId() }, ...(content.length > 0 ? { content } : {}) };
}

export function buildBlock(op: number, spec: unknown, newId: () => string, limits: Limits, counter: { chars: number }): PMJson {
  if (!isObj(spec) || typeof spec.type !== "string") throw new Fail(op, "invalid_block", "a block needs a type");
  switch (spec.type) {
    case "paragraph":
      return paragraph(op, spec.inline, newId, limits, counter);
    case "heading": {
      const level = spec.level;
      if (level !== 1 && level !== 2 && level !== 3) throw new Fail(op, "invalid_block", "heading level must be 1, 2 or 3");
      const content = inlineNodes(op, spec.inline, [], limits, counter);
      return { type: "heading", attrs: { id: newId(), level }, ...(content.length > 0 ? { content } : {}) };
    }
    case "quote": {
      if (!Array.isArray(spec.paragraphs) || spec.paragraphs.length === 0) throw new Fail(op, "invalid_block", "a quote needs at least one paragraph");
      return { type: "blockquote", attrs: { id: newId() }, content: spec.paragraphs.map((p) => paragraph(op, p, newId, limits, counter)) };
    }
    case "bullet_list":
    case "numbered_list":
      return buildList(op, spec, newId, limits, counter);
    case "table": {
      const rows = spec.rows;
      if (!Array.isArray(rows) || rows.length === 0) throw new Fail(op, "invalid_block", "a table needs rows");
      if (rows.length > limits.maxTableRows) throw new Fail(op, "table_too_big", `at most ${limits.maxTableRows} rows`);
      const width = Array.isArray(rows[0]) ? rows[0].length : 0;
      if (width === 0 || width > limits.maxTableCols) throw new Fail(op, "table_too_big", `1 to ${limits.maxTableCols} columns`);
      const header = spec.header_row === true;
      return {
        type: "table",
        attrs: { id: newId() },
        content: rows.map((row, r) => {
          if (!Array.isArray(row) || row.length !== width) throw new Fail(op, "table_not_rectangular", "every row needs the same number of cells");
          return {
            type: "tableRow",
            attrs: { id: newId() },
            content: row.map((cell) => ({ type: header && r === 0 ? "tableHeader" : "tableCell", attrs: { id: newId() }, content: [paragraph(op, cell, newId, limits, counter)] })),
          };
        }),
      };
    }
    case "diagram": {
      if (typeof spec.mermaid !== "string") throw new Fail(op, "invalid_block", "a diagram needs its source");
      const source = spec.mermaid.replace(/\r\n/g, "\n").replace(/^\n+|\n+$/g, "");
      const check = validateMermaidSource(source);
      if (!check.ok) throw new Fail(op, `diagram_${check.code}`, check.message);
      counter.chars += source.length;
      return { type: "diagram", attrs: { id: newId() }, content: [{ type: "text", text: source }] };
    }
    default:
      throw new Fail(op, "invalid_block", `unknown block type ${JSON.stringify(spec.type)}`);
  }
}

function buildList(op: number, spec: Record<string, unknown>, newId: () => string, limits: Limits, counter: { chars: number }): PMJson {
  const listType = spec.type === "numbered_list" ? "orderedList" : "bulletList";
  const items = spec.items;
  if (!Array.isArray(items) || items.length === 0) throw new Fail(op, "invalid_block", "a list needs items");
  const root: PMJson = { type: listType, attrs: { id: newId() }, content: [] };
  // stack[d] = the list node at depth d
  const stack: PMJson[] = [root];
  let lastDepth = -1;
  for (const raw of items) {
    if (!isObj(raw) || !Number.isInteger(raw.depth) || (raw.depth as number) < 0) throw new Fail(op, "invalid_block", "a list item needs a depth");
    const depth = raw.depth as number;
    if (depth + 1 > limits.maxListDepth) throw new Fail(op, "list_too_deep", `lists nest at most ${limits.maxListDepth} deep`);
    if (depth > lastDepth + 1 || (lastDepth === -1 && depth !== 0)) throw new Fail(op, "invalid_block", "a list item can be at most one level deeper than the one before");
    const item: PMJson = { type: "listItem", attrs: { id: newId() }, content: [paragraph(op, raw.inline, newId, limits, counter)] };
    if (depth > 0) {
      const parentList = stack[depth - 1]!;
      const parentItem = parentList.content![parentList.content!.length - 1]!;
      let nested = stack[depth];
      if (!nested || !parentItem.content!.includes(nested)) {
        nested = { type: listType, attrs: { id: newId() }, content: [] };
        parentItem.content!.push(nested);
        stack[depth] = nested;
      }
      nested.content!.push(item);
    } else {
      root.content!.push(item);
    }
    stack.length = depth + 1;
    if (depth === 0) stack[0] = root;
    lastDepth = depth;
  }
  return root;
}

// ---- finding things ---------------------------------------------------------------------------------------

interface Hit {
  node: PMJson;
  parent: PMJson;
  index: number;
  ancestors: PMJson[];
}

function find(doc: PMJson, id: string): Hit | null {
  const walk = (parent: PMJson, ancestors: PMJson[]): Hit | null => {
    const kids = parent.content ?? [];
    for (let i = 0; i < kids.length; i++) {
      const node = kids[i]!;
      if (node.attrs?.id === id) return { node, parent, index: i, ancestors };
      const inner = walk(node, [...ancestors, node]);
      if (inner) return inner;
    }
    return null;
  };
  return walk(doc, []);
}

const idsInside = (node: PMJson): string[] => {
  const out: string[] = [];
  const walk = (n: PMJson) => {
    if (typeof n.attrs?.id === "string") out.push(n.attrs.id);
    (n.content ?? []).forEach(walk);
  };
  walk(node);
  return out;
};

// ---- the operations ---------------------------------------------------------------------------------------

/** Cut inline content at two UTF-16 offsets: [before, middle, after]. Text nodes split; atoms are one unit. */
function cutInline(nodes: PMJson[], start: number, end: number): [PMJson[], PMJson[], PMJson[]] {
  const before: PMJson[] = [];
  const middle: PMJson[] = [];
  const after: PMJson[] = [];
  let at = 0;
  const push = (list: PMJson[], node: PMJson, text?: string) => {
    if (text === undefined) list.push(node);
    else if (text) list.push({ ...node, text });
  };
  for (const node of nodes) {
    const len = node.type === "text" ? (node.text ?? "").length : 1;
    const from = at;
    const to = at + len;
    at = to;
    if (node.type !== "text") {
      push(to <= start ? before : from >= end ? after : middle, node);
      continue;
    }
    const text = node.text ?? "";
    push(before, node, text.slice(0, Math.max(0, Math.min(len, start - from))));
    push(middle, node, text.slice(Math.max(0, start - from), Math.max(0, Math.min(len, end - from))));
    push(after, node, text.slice(Math.max(0, Math.min(len, end - from))));
  }
  return [before, middle, after];
}

export function applyOperations(doc: PMJson, operations: unknown, options: ApplyOptions): OpResult {
  const limits = options.limits ?? DEFAULT_LIMITS;
  const scope = options.scope;
  const counter = { chars: 0 };
  try {
    if (!Array.isArray(operations) || operations.length === 0) throw new Fail(null, "no_operations", "a proposal needs at least one operation");
    if (operations.length > MAX_OPERATIONS) throw new Fail(null, "too_many_operations", `at most ${MAX_OPERATIONS} operations`);
    const work = structuredClone(doc);
    const changes: Change[] = [];
    const touched = new Set<string>();
    const deleted = new Set<string>();
    const insertedAfter = new Map<string, number>();

    operations.forEach((raw, i) => {
      if (!isObj(raw) || typeof raw.op !== "string") throw new Fail(i, "invalid_operation", "an operation needs an op name");
      const inScope = (id: string) => scope?.wholeIds == null || scope.wholeIds.has(id);
      const claim = (id: string) => {
        if (touched.has(id) || deleted.has(id)) throw new Fail(i, "overlapping_operations", "a block can be the target of one operation only");
        touched.add(id);
      };
      switch (raw.op) {
        case "replace_block_text":
        case "replace_range": {
          const id = raw.block_id;
          if (typeof id !== "string") throw new Fail(i, "invalid_operation", "block_id is required");
          if (deleted.has(id)) throw new Fail(i, "overlapping_operations", "the block is deleted by another operation");
          const hit = find(work, id);
          if (!hit) throw new Fail(i, "block_not_found", "the block does not exist in the document");
          if (!holdsText(hit.node)) throw new Fail(i, "not_a_text_block", "only paragraphs, headings and diagrams hold text");
          claim(id);
          if (hit.ancestors.some((a) => deleted.has(String(a.attrs?.id)))) throw new Fail(i, "overlapping_operations", "the block is inside a deleted block");
          const before = structuredClone(hit.node);
          if (hit.node.type === "diagram") {
            // a diagram's source is its text: the model gives it as plain runs without marks
            const source = (raw.new_inline as Run[] | undefined)?.map((r) => r.text).join("") ?? "";
            if (!inScope(id) && !(scope?.ranges ?? []).some((r) => r.block_id === id)) throw new Fail(i, "out_of_scope", "the block is outside the declared scope");
            if (raw.op === "replace_range") throw new Fail(i, "not_a_text_block", "replace a whole diagram, not a range of it");
            const check = validateMermaidSource(source.replace(/\r\n/g, "\n"));
            if (!check.ok) throw new Fail(i, `diagram_${check.code}`, check.message);
            counter.chars += source.length;
            hit.node.content = [{ type: "text", text: source.replace(/\r\n/g, "\n") }];
          } else if (raw.op === "replace_block_text") {
            if (!inScope(id)) throw new Fail(i, "out_of_scope", "the block is outside the declared scope");
            const content = inlineNodes(i, raw.new_inline, inheritedMarksOf(hit.node.content), limits, counter);
            if (content.length > 0) hit.node.content = content;
            else delete hit.node.content;
          } else {
            const start = raw.start;
            const end = raw.end;
            if (!Number.isInteger(start) || !Number.isInteger(end)) throw new Fail(i, "bad_range", "start and end must be integers");
            const whole = scope?.wholeIds == null || scope.wholeIds.has(id);
            const declared = (scope?.ranges ?? []).some((r) => r.block_id === id && r.start <= (start as number) && (end as number) <= r.end);
            if (!whole && !declared) throw new Fail(i, "range_out_of_scope", "the range is outside the declared selection");
            try {
              utf16Slice(blockUnits(hit.node), start as number, end as number);
            } catch (e) {
              if (e instanceof RangeError_) throw new Fail(i, "bad_range", e.message);
              throw e;
            }
            const [pre, mid, post] = cutInline(hit.node.content ?? [], start as number, end as number);
            const content = inlineNodes(i, raw.new_inline, inheritedMarksOf(mid), limits, counter);
            const all = [...pre, ...content, ...post];
            if (all.length > 0) hit.node.content = all;
            else delete hit.node.content;
          }
          changes.push({ op: i, kind: raw.op, block_id: id, anchor: null, before: [before], after: [structuredClone(hit.node)] });
          return;
        }
        case "insert_blocks_after": {
          const anchor = raw.anchor_block_id;
          if (typeof anchor !== "string") throw new Fail(i, "invalid_operation", "anchor_block_id is required");
          if (scope?.anchors != null && !scope.anchors.includes(anchor)) throw new Fail(i, "anchor_not_allowed", "new blocks can only go after a declared anchor");
          if (deleted.has(anchor)) throw new Fail(i, "overlapping_operations", "the anchor is deleted by another operation");
          if (!Array.isArray(raw.blocks) || raw.blocks.length === 0) throw new Fail(i, "invalid_operation", "blocks must be a non-empty list");
          const top = work.content ?? (work.content = []);
          let position: number;
          if (anchor === START) position = 0;
          else {
            const index = top.findIndex((n) => n.attrs?.id === anchor);
            if (index === -1) throw new Fail(i, find(work, anchor) ? "anchor_not_top_level" : "block_not_found", "the anchor must be a top-level block of the document");
            position = index + 1;
          }
          position += insertedAfter.get(anchor) ?? 0;
          const built = raw.blocks.map((b) => buildBlock(i, b, options.newId, limits, counter));
          top.splice(position, 0, ...built);
          insertedAfter.set(anchor, (insertedAfter.get(anchor) ?? 0) + built.length);
          changes.push({ op: i, kind: "insert_blocks_after", block_id: null, anchor, before: [], after: structuredClone(built) });
          return;
        }
        case "delete_blocks": {
          const ids = raw.block_ids;
          if (!Array.isArray(ids) || ids.length === 0 || ids.some((x) => typeof x !== "string")) throw new Fail(i, "invalid_operation", "block_ids must be a non-empty list of ids");
          for (const id of ids as string[]) {
            if (!inScope(id)) throw new Fail(i, "out_of_scope", "the block is outside the declared scope");
            if (deleted.has(id) || touched.has(id)) throw new Fail(i, "overlapping_operations", "a block can be the target of one operation only");
            const hit = find(work, id);
            if (!hit) throw new Fail(i, "block_not_found", "the block does not exist in the document");
            claim(id);
            if (hit.ancestors.some((a) => deleted.has(String(a.attrs?.id)))) throw new Fail(i, "overlapping_operations", "the block is inside a deleted block");
            if (insertedAfter.has(id)) throw new Fail(i, "overlapping_operations", "a block that received new blocks cannot be deleted");
            const inner = idsInside(hit.node);
            if (inner.some((x) => x !== id && touched.has(x))) throw new Fail(i, "overlapping_operations", "the block contains another target");
            const before = structuredClone(hit.node);
            hit.parent.content!.splice(hit.index, 1);
            if (hit.parent.content!.length === 0) delete hit.parent.content;
            inner.forEach((x) => deleted.add(x));
            changes.push({ op: i, kind: "delete_blocks", block_id: id, anchor: null, before: [before], after: [] });
          }
          return;
        }
        default:
          throw new Fail(i, "unknown_operation", `unknown operation ${JSON.stringify(raw.op)}`);
      }
    });

    if (counter.chars > MAX_INSERTED_CHARS) throw new Fail(null, "too_much_text", `at most ${MAX_INSERTED_CHARS} inserted characters`);
    // The result must be a valid document. "structure": a diagram a person left half-typed elsewhere in the
    // document must not block an unrelated edit; the diagrams THIS proposal writes were checked strictly above.
    const checked = validateDocument(canonicalize(work), limits, undefined, "structure");
    if (!checked.ok) {
      const first = checked.errors[0]!;
      return { ok: false, errors: [{ op: null, code: `candidate_${first.code}`, message: first.message, path: first.path }] };
    }
    // the preview shows blocks in canonical form (adjacent runs with the same marks merged), like the stored document
    for (const change of changes) {
      change.before = change.before.map((n) => canonicalize(n));
      change.after = change.after.map((n) => canonicalize(n));
    }
    return { ok: true, doc: checked.doc, changes, insertedChars: counter.chars };
  } catch (error) {
    if (error instanceof Fail) return { ok: false, errors: [{ op: error.op, code: error.code, message: error.message }] };
    throw error;
  }
}

// ---- a draft ----------------------------------------------------------------------------------------------

export interface Draft {
  title: string;
  blocks: NewBlock[];
}

/** A whole new document from a draft's blocks (validated strictly: it enters a document from outside). */
export function buildDraftDocument(draft: Draft, options: { newId: () => string; limits?: Limits }): OpResult {
  const limits = options.limits ?? DEFAULT_LIMITS;
  const counter = { chars: 0 };
  try {
    if (!Array.isArray(draft.blocks) || draft.blocks.length === 0) throw new Fail(null, "no_operations", "a draft needs at least one block");
    const blocks = draft.blocks.map((b, i) => buildBlock(i, b, options.newId, limits, counter));
    if (counter.chars > MAX_INSERTED_CHARS) throw new Fail(null, "too_much_text", `at most ${MAX_INSERTED_CHARS} characters`);
    const checked = validateDocument(canonicalize({ type: "doc", content: blocks }), limits, undefined, "strict");
    if (!checked.ok) {
      const first = checked.errors[0]!;
      return { ok: false, errors: [{ op: null, code: `candidate_${first.code}`, message: first.message, path: first.path }] };
    }
    return { ok: true, doc: checked.doc, changes: [], insertedChars: counter.chars };
  } catch (error) {
    if (error instanceof Fail) return { ok: false, errors: [{ op: error.op, code: error.code, message: error.message }] };
    throw error;
  }
}
