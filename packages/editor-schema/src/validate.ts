/**
 * validateDocument(json): is this ProseMirror JSON a document our schema AND our rules accept?
 *
 * Three layers, all required:
 *  1. RULES walk over the INPUT JSON (so error paths point into what the caller sent): value domains
 *     (colours, font keys, spacing, indent...), the link policy, ids, list nesting, table shape, and the
 *     size limits. Never truncates; reports the paths of up to MAX_ERRORS problems.
 *  2. The ProseMirror STRUCTURAL check: schema.nodeFromJSON(json).check() (content expressions such as
 *     "a list item starts with a paragraph").
 *  3. DROP detection: nodeFromJSON silently ignores attributes the schema does not know, so the input is
 *     compared with the node's own JSON; any difference means the schema would lose something, which is
 *     reported (`would_be_dropped`) instead of accepted.
 * The returned `doc` is the canonical form (canonical.ts), the thing to store and hash.
 */
import type { Schema } from "@tiptap/pm/model";
import { type PMJson, canonicalStringify, canonicalize, firstDifference } from "./canonical.js";
import { isHexColor } from "./colors.js";
import { ID_TYPES } from "./extensions.js";
import { ALIGNMENTS, DEFAULT_LIMITS, LINE_SPACINGS, type Limits, MAX_FONT_PT, MAX_INDENT_LEVEL, MAX_SPACE_PT, MIN_FONT_PT } from "./limits.js";
import { checkLink } from "./links.js";
import { getEditorSchema } from "./schema.js";

export interface DocError {
  path: string;
  code: string;
  message: string;
}
export interface DocStats {
  textCodepoints: number;
  nodes: number;
  blocks: number;
  images: number;
  bytes: number;
}
export type ValidationResult = { ok: true; doc: PMJson; stats: DocStats } | { ok: false; errors: DocError[] };

const MAX_ERRORS = 20;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const BLOCKS = new Set(["paragraph", "heading", "blockquote", "bulletList", "orderedList", "table"]);
const ID_SET = new Set<string>(ID_TYPES);
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

export const isUuid = (v: unknown): v is string => typeof v === "string" && UUID_RE.test(v);

function codePoints(s: string): number {
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) i++;
    n++;
  }
  return n;
}

function badText(s: string): string | null {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if ((c < 0x20 && c !== 9) || (c >= 0x7f && c <= 0x9f) || c === 0x2028 || c === 0x2029) return "contains a newline or control character";
    if (c >= 0xd800 && c <= 0xdbff) {
      const next = s.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) i++;
      else return "contains a lone surrogate";
    } else if (c >= 0xdc00 && c <= 0xdfff) return "contains a lone surrogate";
  }
  return null;
}

class Walk {
  errors: DocError[] = [];
  ids = new Set<string>();
  stats = { textCodepoints: 0, nodes: 0, blocks: 0, images: 0 };
  constructor(
    readonly limits: Limits,
    readonly schema: Schema,
  ) {}

  err(path: string, code: string, message: string): void {
    if (this.errors.length < MAX_ERRORS) this.errors.push({ path, code, message });
  }
  get full(): boolean {
    return this.errors.length >= MAX_ERRORS;
  }

  node(n: unknown, path: string, parents: string[]): void {
    if (this.full) return;
    if (!isObject(n)) return this.err(path, "invalid_type", "expected an object");
    const type = n.type;
    if (typeof type !== "string" || !this.schema.nodes[type]) return this.err(`${path}/type`, "unknown_node", `unknown node type ${JSON.stringify(type)}`);
    this.stats.nodes++;
    if (this.stats.nodes > this.limits.maxTotalNodes) return this.err(path, "limit_exceeded", `more than ${this.limits.maxTotalNodes} nodes`);
    if (BLOCKS.has(type) || type === "listItem") {
      this.stats.blocks++;
      if (this.stats.blocks > this.limits.maxBlocks) return this.err(path, "limit_exceeded", `more than ${this.limits.maxBlocks} blocks and list items`);
    }
    const attrs = isObject(n.attrs) ? n.attrs : {};
    if (ID_SET.has(type)) this.id(attrs.id, `${path}/attrs/id`);

    switch (type) {
      case "text":
        return this.text(n, path);
      case "paragraph":
      case "heading":
        this.paragraphAttrs(type, attrs, path, parents);
        break;
      case "bulletList":
      case "orderedList": {
        const depth = parents.filter((p) => p === "bulletList" || p === "orderedList").length + 1;
        if (depth > this.limits.maxListDepth) this.err(path, "limit_exceeded", `lists nest at most ${this.limits.maxListDepth} deep`);
        if (type === "orderedList") {
          if (attrs.start !== undefined && attrs.start !== null && attrs.start !== 1) this.err(`${path}/attrs/start`, "invalid_value", "custom start numbers are not supported");
          if (attrs.type !== undefined && attrs.type !== null) this.err(`${path}/attrs/type`, "invalid_value", "list numbering styles are not supported");
        }
        break;
      }
      case "table":
        this.table(n, path);
        break;
      case "tableCell":
      case "tableHeader":
        this.cell(attrs, path);
        break;
      case "image":
        this.image(attrs, path);
        break;
      default:
        break;
    }
    if (Array.isArray(n.content)) {
      const next = [...parents, type];
      n.content.forEach((child, i) => this.node(child, `${path}/content/${i}`, next));
    } else if (n.content !== undefined) this.err(`${path}/content`, "invalid_type", "expected an array");
  }

  id(value: unknown, path: string): void {
    if (!isUuid(value)) return this.err(path, "invalid_id", "every block needs a UUID id");
    const lower = value.toLowerCase();
    if (this.ids.has(lower)) return this.err(path, "duplicate_id", "id is used more than once in the document");
    this.ids.add(lower);
  }

  paragraphAttrs(type: string, attrs: Record<string, unknown>, path: string, parents: string[]): void {
    if (type === "heading") {
      const level = attrs.level ?? 1; // level 1 is the schema default and is omitted in canonical form
      if (level !== 1 && level !== 2 && level !== 3) this.err(`${path}/attrs/level`, "invalid_value", "heading level must be 1, 2 or 3");
      if (parents.some((p) => p !== "doc")) this.err(path, "invalid_structure", "headings can only be in the body of the document");
    }
    const a = attrs.textAlign;
    if (a !== undefined && a !== null && !(ALIGNMENTS as readonly unknown[]).includes(a)) this.err(`${path}/attrs/textAlign`, "invalid_value", `alignment must be one of ${ALIGNMENTS.join(", ")}`);
    const ls = attrs.lineSpacing;
    if (ls !== undefined && ls !== null && !(typeof ls === "number" && (LINE_SPACINGS as readonly number[]).includes(ls))) this.err(`${path}/attrs/lineSpacing`, "invalid_value", "line spacing must be 1, 1.15, 1.5 or 2");
    for (const key of ["spaceBefore", "spaceAfter"] as const) {
      const v = attrs[key];
      if (v !== undefined && v !== null && !(typeof v === "number" && v >= 0 && v <= MAX_SPACE_PT && Number.isInteger(v * 2))) this.err(`${path}/attrs/${key}`, "invalid_value", "spacing must be 0 to 72 points in steps of 0.5");
    }
    const ind = attrs.indent;
    if (ind !== undefined && ind !== null) {
      if (!(typeof ind === "number" && Number.isInteger(ind) && ind >= 0 && ind <= MAX_INDENT_LEVEL)) this.err(`${path}/attrs/indent`, "invalid_value", "indent must be an integer 0 to 8");
      else if (ind !== 0 && parents.includes("listItem")) this.err(`${path}/attrs/indent`, "invalid_value", "indent must be 0 inside list items (list nesting indents)");
    }
  }

  table(n: Record<string, unknown>, path: string): void {
    const rows = Array.isArray(n.content) ? n.content : [];
    if (rows.length > this.limits.maxTableRows) this.err(`${path}/content`, "limit_exceeded", `at most ${this.limits.maxTableRows} rows`);
    let width = -1;
    rows.forEach((row, r) => {
      const cells = isObject(row) && Array.isArray(row.content) ? row.content : [];
      if (cells.length > this.limits.maxTableCols) this.err(`${path}/content/${r}/content`, "limit_exceeded", `at most ${this.limits.maxTableCols} columns`);
      if (width === -1) width = cells.length;
      else if (cells.length !== width) this.err(`${path}/content/${r}/content`, "invalid_structure", "rows must have the same number of cells");
    });
  }

  cell(attrs: Record<string, unknown>, path: string): void {
    for (const key of ["colspan", "rowspan"] as const) {
      const v = attrs[key];
      if (v !== undefined && v !== null && v !== 1) this.err(`${path}/attrs/${key}`, "invalid_value", "merged table cells are not supported");
    }
    if (attrs.colwidth !== undefined && attrs.colwidth !== null) this.err(`${path}/attrs/colwidth`, "invalid_value", "column widths are not supported");
  }

  image(attrs: Record<string, unknown>, path: string): void {
    this.stats.images++;
    if (this.stats.images > this.limits.maxImages) this.err(path, "limit_exceeded", `more than ${this.limits.maxImages} images`);
    if (!isUuid(attrs.assetId)) this.err(`${path}/attrs/assetId`, "invalid_id", "an image needs an asset id (a UUID)");
    const alt = attrs.alt;
    if (alt !== undefined && alt !== null) {
      if (typeof alt !== "string") this.err(`${path}/attrs/alt`, "invalid_type", "alt text must be a string");
      else {
        if (codePoints(alt) > this.limits.maxAltCodepoints) this.err(`${path}/attrs/alt`, "limit_exceeded", `alt text is limited to ${this.limits.maxAltCodepoints} characters`);
        const bad = badText(alt);
        if (bad) this.err(`${path}/attrs/alt`, "invalid_text", `alt text ${bad}`);
      }
    }
    for (const key of ["width", "height"] as const) {
      const v = attrs[key];
      if (!(typeof v === "number" && Number.isInteger(v) && v >= 1 && v <= this.limits.maxImagePx)) this.err(`${path}/attrs/${key}`, "invalid_value", `image ${key} must be an integer 1 to ${this.limits.maxImagePx}`);
    }
  }

  text(n: Record<string, unknown>, path: string): void {
    const text = n.text;
    if (typeof text !== "string" || text === "") return this.err(`${path}/text`, "invalid_text", "text nodes need non-empty text");
    const bad = badText(text);
    if (bad) this.err(`${path}/text`, "invalid_text", `text ${bad}`);
    this.stats.textCodepoints += codePoints(text);
    if (this.stats.textCodepoints > this.limits.maxTextCodepoints) this.err(`${path}/text`, "limit_exceeded", `more than ${this.limits.maxTextCodepoints} characters of text`);
    if (n.marks === undefined) return;
    if (!Array.isArray(n.marks)) return this.err(`${path}/marks`, "invalid_type", "marks must be an array");
    n.marks.forEach((m, i) => this.mark(m, `${path}/marks/${i}`));
  }

  mark(m: unknown, path: string): void {
    if (!isObject(m) || typeof m.type !== "string" || !this.schema.marks[m.type]) return this.err(`${path}/type`, "unknown_mark", `unknown mark ${JSON.stringify(isObject(m) ? m.type : m)}`);
    const attrs = isObject(m.attrs) ? m.attrs : {};
    switch (m.type) {
      case "link": {
        const href = attrs.href;
        if (typeof href !== "string") return this.err(`${path}/attrs/href`, "invalid_link", "a link needs an address");
        if (checkLink(href, this.limits.maxLinkLength) === null) this.err(`${path}/attrs/href`, "invalid_link", "only http, https and mailto links are allowed");
        break;
      }
      case "textStyle": {
        const font = attrs.fontKey;
        if (font !== undefined && font !== null && !(typeof font === "string" && this.limits.allowedFonts.includes(font))) this.err(`${path}/attrs/fontKey`, "invalid_value", "not an allowed font key");
        const size = attrs.fontSizePt;
        if (size !== undefined && size !== null && !(typeof size === "number" && size >= MIN_FONT_PT && size <= MAX_FONT_PT && Number.isInteger(size * 2))) this.err(`${path}/attrs/fontSizePt`, "invalid_value", "font size must be 8 to 72 in steps of 0.5");
        const color = attrs.color;
        if (color !== undefined && color !== null && !isHexColor(color)) this.err(`${path}/attrs/color`, "invalid_value", "colours look like #RRGGBB");
        break;
      }
      case "highlight":
        if (!isHexColor(attrs.color ?? "#FFFF00")) this.err(`${path}/attrs/color`, "invalid_value", "highlight colours look like #RRGGBB");
        break;
      default:
        break;
    }
  }
}

export function validateDocument(json: unknown, limits: Limits = DEFAULT_LIMITS, schema: Schema = getEditorSchema()): ValidationResult {
  if (!isObject(json) || json.type !== "doc") return { ok: false, errors: [{ path: "", code: "invalid_type", message: "expected a document node" }] };
  const input = json as PMJson;

  const bytes = new TextEncoder().encode(canonicalStringify(canonicalize(input, schema))).length;
  if (bytes > limits.maxSerializedBytes) return { ok: false, errors: [{ path: "", code: "limit_exceeded", message: `serialized content is larger than ${limits.maxSerializedBytes} bytes` }] };

  const walk = new Walk(limits, schema);
  walk.node(json, "", []);
  if (walk.errors.length > 0) return { ok: false, errors: walk.errors };

  let pm;
  try {
    pm = schema.nodeFromJSON(json);
    pm.check();
  } catch (e) {
    const message = e instanceof Error ? e.message.slice(0, 160) : "invalid structure";
    return { ok: false, errors: [{ path: "", code: "invalid_structure", message }] };
  }

  const canonicalInput = canonicalize(input, schema);
  const canonicalNode = canonicalize(pm.toJSON() as PMJson, schema);
  const diff = firstDifference(canonicalInput, canonicalNode);
  if (diff) return { ok: false, errors: [{ path: diff, code: "would_be_dropped", message: "this content is not part of the schema and would be lost" }] };

  return { ok: true, doc: canonicalNode, stats: { ...walk.stats, bytes } };
}
