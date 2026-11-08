/**
 * The document model: validation AND canonicalization in one pass. The TypeScript twin of
 * backend/app/documents/schema.py: same algorithm, same traversal order, so both report the same
 * FIRST error for the same input. Both are run against shared/fixtures/ (see
 * client/test/document-schema.test.ts and backend/tests/test_document_schema.py). Keep them in step.
 * The specification is docs/document-schema.md.
 *
 * The editor canonicalizes before saving, so the server normally has nothing to change.
 */

export const SCHEMA_VERSION = 1;

export const DEFAULT_FONT_KEYS: readonly string[] = [
  "sans",
  "serif",
  "mono",
  "calibri",
  "cambria",
  "arial",
  "times_new_roman",
  "courier_new",
];

export interface Limits {
  maxSerializedBytes: number;
  maxTextCodepoints: number;
  maxTotalNodes: number;
  maxBlocks: number; // block nodes (anywhere) plus list items
  maxInlineNodes: number;
  maxImages: number;
  maxListDepth: number;
  maxLinkLength: number;
  allowedFonts: readonly string[];
}

export const DEFAULT_LIMITS: Limits = {
  maxSerializedBytes: 1024 * 1024,
  maxTextCodepoints: 100_000,
  maxTotalNodes: 60_000,
  maxBlocks: 5_000,
  maxInlineNodes: 20_000,
  maxImages: 100,
  maxListDepth: 3,
  maxLinkLength: 2048,
  allowedFonts: DEFAULT_FONT_KEYS,
};

export const ALIGNS = ["left", "center", "right", "justify"] as const;
export const LINE_SPACINGS = [1, 1.15, 1.5, 2] as const;
export const SCRIPTS = ["superscript", "subscript"] as const;
const LINK_SCHEMES = ["http", "https", "mailto"];
export const MAX_TABLE_ROWS = 50;
export const MAX_TABLE_COLUMNS = 20;
export const MAX_ALT_CODEPOINTS = 500;
export const MAX_IMAGE_PX = 10_000;
export const MAX_INDENT_LEVEL = 8;

const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const COLOR_RE = /^#[0-9a-fA-F]{6}$/;
const SCHEME_RE = /^([A-Za-z][A-Za-z0-9+.-]*):/;

// ---- JSON shapes (loose on purpose: validation is what makes them trustworthy) ---------------------------
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Obj = { [key: string]: unknown };

export interface Marks {
  bold?: true;
  italic?: true;
  underline?: true;
  strike?: true;
  script?: "superscript" | "subscript";
  font?: string;
  size_pt?: number;
  color?: string;
  highlight?: string;
  link?: { href: string };
}
export interface TextNode {
  type: "text";
  text: string;
  marks?: Marks;
}
export interface HardBreak {
  type: "hard_break";
}
export interface ImageNode {
  type: "image";
  id: string;
  asset_id: string;
  width_px: number;
  height_px: number;
  alt?: string;
}
export type Inline = TextNode | HardBreak | ImageNode;
export interface ParagraphAttrs {
  align?: "center" | "right" | "justify";
  line_spacing?: number;
  space_before_pt?: number;
  space_after_pt?: number;
  indent_level?: number;
}
export interface Paragraph {
  type: "paragraph";
  id: string;
  attrs?: ParagraphAttrs;
  children?: Inline[];
}
export interface Heading {
  type: "heading";
  id: string;
  level: 1 | 2 | 3;
  attrs?: ParagraphAttrs;
  children?: Inline[];
}
export interface Quote {
  type: "quote";
  id: string;
  children: Paragraph[];
}
export interface ListItem {
  type: "list_item";
  id: string;
  children: (Paragraph | List)[];
}
export interface List {
  type: "list";
  id: string;
  ordered?: true;
  children: ListItem[];
}
export interface TableCell {
  type: "table_cell";
  id: string;
  header?: true;
  children: Paragraph[];
}
export interface TableRow {
  type: "table_row";
  id: string;
  children: TableCell[];
}
export interface Table {
  type: "table";
  id: string;
  children: TableRow[];
}
export type Block = Paragraph | Heading | Quote | List | Table;
export interface DocumentContent {
  schema_version: 1;
  blocks: Block[];
}

export class InvalidDocument extends Error {
  constructor(
    readonly path: string,
    readonly code: string,
    readonly detail: string,
  ) {
    super(`${path || "/"}: ${code}: ${detail}`);
  }
}

export interface ValidatedDocument {
  content: DocumentContent;
  assetIds: Set<string>;
  stats: { text_codepoints: number; nodes: number; blocks: number; inline_nodes: number; images: number; bytes: number };
}

function fail(path: string, code: string, detail: string): never {
  throw new InvalidDocument(path, code, detail);
}

// ---- scalar helpers ------------------------------------------------------------------------------------------

export function isBadUrlChar(code: number): boolean {
  return (
    code <= 0x20 ||
    (code >= 0x7f && code <= 0xa0) ||
    code === 0x180e ||
    code === 0x1680 ||
    code === 0x3000 ||
    code === 0xfeff ||
    (code >= 0x2000 && code <= 0x200f) ||
    (code >= 0x2028 && code <= 0x202f) ||
    (code >= 0x205f && code <= 0x206f)
  );
}

function codepoints(s: string): string[] {
  return Array.from(s);
}

/** The canonical href, or null if the link policy rejects it. */
export function checkLink(href: string, maxLength: number): string | null {
  const chars = codepoints(href);
  let start = 0;
  let end = chars.length;
  while (start < end && isBadUrlChar(chars[start]!.codePointAt(0)!)) start++;
  while (end > start && isBadUrlChar(chars[end - 1]!.codePointAt(0)!)) end--;
  const kept = chars.slice(start, end);
  if (kept.length === 0 || kept.length > maxLength) return null;
  if (kept.some((c) => isBadUrlChar(c.codePointAt(0)!))) return null;
  const s = kept.join("");
  const match = SCHEME_RE.exec(s);
  if (!match) return null;
  const scheme = match[1]!.toLowerCase();
  if (!LINK_SCHEMES.includes(scheme)) return null;
  const rest = s.slice(match[0].length);
  if (scheme === "mailto") {
    if (!rest) return null;
  } else {
    if (!rest.startsWith("//") || s.includes("\\")) return null;
    const authority = rest.slice(2).split(/[/?#]/, 1)[0]!;
    if (!authority || authority.includes("@")) return null;
  }
  return scheme + ":" + rest;
}

/** A message, or null when the (already trimmed) title is acceptable. */
export function titleError(title: string): string | null {
  if (!title || title !== title.trim()) return "must not be empty or have leading/trailing whitespace";
  if (codepoints(title).length > 200) return "must be at most 200 characters";
  for (let i = 0; i < title.length; i++) {
    const o = title.charCodeAt(i);
    if (o < 0x20 || (o >= 0x7f && o <= 0x9f) || (o >= 0xd800 && o <= 0xdfff) || (o >= 0x202a && o <= 0x202e) || (o >= 0x2066 && o <= 0x2069)) {
      // a well-formed surrogate PAIR is fine (emoji); only lone surrogates are refused
      if (o >= 0xd800 && o <= 0xdfff && isPairedSurrogate(title, i)) {
        i++;
        continue;
      }
      return "must not contain control characters";
    }
  }
  return null;
}

function isPairedSurrogate(s: string, i: number): boolean {
  const hi = s.charCodeAt(i);
  const lo = s.charCodeAt(i + 1);
  return hi >= 0xd800 && hi <= 0xdbff && lo >= 0xdc00 && lo <= 0xdfff;
}

function hasLoneSurrogate(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const next = s.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        i++;
        continue;
      }
      return true;
    }
    if (c >= 0xdc00 && c <= 0xdfff) return true;
  }
  return false;
}

function isNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}
function isInteger(v: unknown): v is number {
  return isNumber(v) && Number.isInteger(v);
}
function isObject(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
const has = (o: Obj, key: string): boolean => Object.prototype.hasOwnProperty.call(o, key);

// ---- the walk ------------------------------------------------------------------------------------------------

class Walk {
  ids = new Set<string>();
  assetIds = new Set<string>();
  textCp = 0;
  nodes = 0;
  blocks = 0;
  inline = 0;
  images = 0;

  constructor(readonly limits: Limits) {}

  node(path: string): void {
    this.nodes++;
    if (this.nodes > this.limits.maxTotalNodes) fail(path, "limit_exceeded", `more than ${this.limits.maxTotalNodes} nodes`);
  }

  block(path: string): void {
    this.node(path);
    this.blocks++;
    if (this.blocks > this.limits.maxBlocks) fail(path, "limit_exceeded", `more than ${this.limits.maxBlocks} blocks and items`);
  }

  obj(value: unknown, path: string, required: string[], optional: string[]): Obj {
    if (!isObject(value)) return fail(path, "invalid_type", "expected an object");
    for (const key of Object.keys(value)) {
      if (!required.includes(key) && !optional.includes(key)) fail(`${path}/${key}`, "unknown_field", "unknown field");
    }
    for (const key of required) {
      if (!has(value, key)) fail(`${path}/${key}`, "missing_field", "required field is missing");
    }
    return value;
  }

  array(value: unknown, path: string, minimum = 0): unknown[] {
    if (!Array.isArray(value)) return fail(path, "invalid_type", "expected an array");
    if (value.length < minimum) fail(path, "invalid_structure", `needs at least ${minimum} item(s)`);
    return value;
  }

  kind(node: unknown, path: string, allowed: string[]): string {
    if (!isObject(node)) return fail(path, "invalid_type", "expected an object");
    const t = node["type"];
    if (typeof t !== "string") return fail(`${path}/type`, "missing_field", "type is required");
    if (!allowed.includes(t)) fail(`${path}/type`, "invalid_structure", `${JSON.stringify(t)} is not allowed here`);
    return t;
  }

  uid(value: unknown, path: string): string {
    if (typeof value !== "string" || !UUID_RE.test(value)) return fail(path, "invalid_id", "must be a UUID");
    const canonical = value.toLowerCase();
    if (this.ids.has(canonical)) fail(path, "duplicate_id", "id is used more than once in the document");
    this.ids.add(canonical);
    return canonical;
  }

  root(raw: unknown): DocumentContent {
    const doc = this.obj(raw, "", ["schema_version", "blocks"], []);
    if (doc["schema_version"] !== SCHEMA_VERSION) fail("/schema_version", "invalid_value", `must be ${SCHEMA_VERSION}`);
    const blocks = this.array(doc["blocks"], "/blocks", 1);
    const out: Block[] = [];
    blocks.forEach((node, i) => {
      const path = `/blocks/${i}`;
      const t = this.kind(node, path, ["paragraph", "heading", "quote", "list", "table"]);
      const o = node as Obj;
      if (t === "paragraph") out.push(this.paragraph(o, path, false));
      else if (t === "heading") out.push(this.heading(o, path));
      else if (t === "quote") out.push(this.quote(o, path));
      else if (t === "list") out.push(this.list(o, path, 1));
      else out.push(this.table(o, path));
    });
    return { schema_version: SCHEMA_VERSION, blocks: out };
  }

  attrs(raw: unknown, path: string, inListItem: boolean): ParagraphAttrs {
    if (raw === undefined || raw === null) return {};
    const a = this.obj(raw, path, [], ["align", "line_spacing", "space_before_pt", "space_after_pt", "indent_level"]);
    const out: ParagraphAttrs = {};
    if (has(a, "align")) {
      if (!(ALIGNS as readonly unknown[]).includes(a["align"])) fail(`${path}/align`, "invalid_value", `must be one of ${ALIGNS.join(", ")}`);
      if (a["align"] !== "left") out.align = a["align"] as "center" | "right" | "justify";
    }
    if (has(a, "line_spacing")) {
      const v = a["line_spacing"];
      if (!isNumber(v) || !(LINE_SPACINGS as readonly number[]).includes(v)) fail(`${path}/line_spacing`, "invalid_value", "must be 1, 1.15, 1.5 or 2");
      if (v !== 1) out.line_spacing = v;
    }
    for (const key of ["space_before_pt", "space_after_pt"] as const) {
      if (has(a, key)) {
        const v = a[key];
        if (!isNumber(v) || v < 0 || v > 72 || !Number.isInteger(v * 2)) fail(`${path}/${key}`, "invalid_value", "must be 0 to 72 in steps of 0.5");
        if (v !== 0) out[key] = v;
      }
    }
    if (has(a, "indent_level")) {
      const v = a["indent_level"];
      if (!isInteger(v) || v < 0 || v > MAX_INDENT_LEVEL) fail(`${path}/indent_level`, "invalid_value", "must be an integer 0 to 8");
      if (inListItem && v !== 0) fail(`${path}/indent_level`, "invalid_value", "must be 0 inside list items");
      if (v !== 0) out.indent_level = v;
    }
    return out;
  }

  paragraph(node: Obj, path: string, inListItem: boolean): Paragraph {
    this.obj(node, path, ["type", "id"], ["attrs", "children"]);
    this.block(path);
    const out: Paragraph = { type: "paragraph", id: this.uid(node["id"], `${path}/id`) };
    const attrs = this.attrs(node["attrs"], `${path}/attrs`, inListItem);
    if (Object.keys(attrs).length > 0) out.attrs = attrs;
    const children = this.inlines(has(node, "children") ? node["children"] : [], `${path}/children`);
    if (children.length > 0) out.children = children;
    return out;
  }

  heading(node: Obj, path: string): Heading {
    this.obj(node, path, ["type", "id", "level"], ["attrs", "children"]);
    this.block(path);
    const level = node["level"];
    if (level !== 1 && level !== 2 && level !== 3) fail(`${path}/level`, "invalid_value", "must be 1, 2 or 3");
    const out: Heading = { type: "heading", id: this.uid(node["id"], `${path}/id`), level };
    const attrs = this.attrs(node["attrs"], `${path}/attrs`, false);
    if (Object.keys(attrs).length > 0) out.attrs = attrs;
    const children = this.inlines(has(node, "children") ? node["children"] : [], `${path}/children`);
    if (children.length > 0) out.children = children;
    return out;
  }

  quote(node: Obj, path: string): Quote {
    this.obj(node, path, ["type", "id", "children"], []);
    this.block(path);
    const id = this.uid(node["id"], `${path}/id`);
    const kids: Paragraph[] = [];
    this.array(node["children"], `${path}/children`, 1).forEach((child, i) => {
      const cpath = `${path}/children/${i}`;
      this.kind(child, cpath, ["paragraph"]);
      kids.push(this.paragraph(child as Obj, cpath, false));
    });
    return { type: "quote", id, children: kids };
  }

  list(node: Obj, path: string, depth: number): List {
    this.obj(node, path, ["type", "id", "children"], ["ordered"]);
    if (depth > this.limits.maxListDepth) fail(path, "limit_exceeded", `lists nest at most ${this.limits.maxListDepth} deep`);
    this.block(path);
    const out: List = { type: "list", id: this.uid(node["id"], `${path}/id`), children: [] };
    if (has(node, "ordered")) {
      if (typeof node["ordered"] !== "boolean") fail(`${path}/ordered`, "invalid_type", "expected a boolean");
      if (node["ordered"]) out.ordered = true;
    }
    this.array(node["children"], `${path}/children`, 1).forEach((item, i) => {
      out.children.push(this.listItem(item, `${path}/children/${i}`, depth));
    });
    // keep the same key set as the Python twin (key order is irrelevant: serialization sorts keys)
    return out;
  }

  listItem(node: unknown, path: string, depth: number): ListItem {
    this.kind(node, path, ["list_item"]);
    const o = this.obj(node, path, ["type", "id", "children"], []);
    this.block(path);
    const id = this.uid(o["id"], `${path}/id`);
    const kids: (Paragraph | List)[] = [];
    this.array(o["children"], `${path}/children`, 1).forEach((child, i) => {
      const cpath = `${path}/children/${i}`;
      if (i === 0) {
        this.kind(child, cpath, ["paragraph"]);
        kids.push(this.paragraph(child as Obj, cpath, true));
      } else {
        const t = this.kind(child, cpath, ["paragraph", "list"]);
        kids.push(t === "paragraph" ? this.paragraph(child as Obj, cpath, true) : this.list(child as Obj, cpath, depth + 1));
      }
    });
    return { type: "list_item", id, children: kids };
  }

  table(node: Obj, path: string): Table {
    this.obj(node, path, ["type", "id", "children"], []);
    this.block(path);
    const id = this.uid(node["id"], `${path}/id`);
    const rowsRaw = this.array(node["children"], `${path}/children`, 1);
    if (rowsRaw.length > MAX_TABLE_ROWS) fail(`${path}/children`, "limit_exceeded", `at most ${MAX_TABLE_ROWS} rows`);
    const rows: TableRow[] = [];
    let width = -1;
    rowsRaw.forEach((row, r) => {
      const rpath = `${path}/children/${r}`;
      this.kind(row, rpath, ["table_row"]);
      const ro = this.obj(row, rpath, ["type", "id", "children"], []);
      this.node(rpath);
      const rowId = this.uid(ro["id"], `${rpath}/id`);
      const cellsRaw = this.array(ro["children"], `${rpath}/children`, 1);
      if (cellsRaw.length > MAX_TABLE_COLUMNS) fail(`${rpath}/children`, "limit_exceeded", `at most ${MAX_TABLE_COLUMNS} columns`);
      if (width === -1) width = cellsRaw.length;
      else if (cellsRaw.length !== width) fail(`${rpath}/children`, "invalid_structure", "rows must have the same cell count");
      const cells = cellsRaw.map((cell, c) => this.cell(cell, `${rpath}/children/${c}`));
      rows.push({ type: "table_row", id: rowId, children: cells });
    });
    return { type: "table", id, children: rows };
  }

  cell(node: unknown, path: string): TableCell {
    this.kind(node, path, ["table_cell"]);
    const o = this.obj(node, path, ["type", "id", "children"], ["header"]);
    this.node(path);
    const out: TableCell = { type: "table_cell", id: this.uid(o["id"], `${path}/id`), children: [] };
    if (has(o, "header")) {
      if (typeof o["header"] !== "boolean") fail(`${path}/header`, "invalid_type", "expected a boolean");
      if (o["header"]) out.header = true;
    }
    this.array(o["children"], `${path}/children`, 1).forEach((child, i) => {
      const cpath = `${path}/children/${i}`;
      this.kind(child, cpath, ["paragraph"]);
      out.children.push(this.paragraph(child as Obj, cpath, false));
    });
    return out;
  }

  inlines(raw: unknown, path: string): Inline[] {
    const out: Inline[] = [];
    this.array(raw, path).forEach((node, i) => {
      const npath = `${path}/${i}`;
      const t = this.kind(node, npath, ["text", "hard_break", "image"]);
      this.node(npath);
      this.inline++;
      if (this.inline > this.limits.maxInlineNodes) fail(npath, "limit_exceeded", `more than ${this.limits.maxInlineNodes} inline nodes`);
      const o = node as Obj;
      if (t === "hard_break") {
        this.obj(o, npath, ["type"], []);
        out.push({ type: "hard_break" });
      } else if (t === "image") {
        out.push(this.image(o, npath));
      } else {
        const text = this.text(o, npath);
        if (text === null) return;
        const last = out[out.length - 1];
        if (last && last.type === "text" && sameMarks(last.marks, text.marks)) last.text += text.text;
        else out.push(text);
      }
    });
    return out;
  }

  text(node: Obj, path: string): TextNode | null {
    this.obj(node, path, ["type", "text"], ["marks"]);
    const value = node["text"];
    if (typeof value !== "string") return fail(`${path}/text`, "invalid_type", "expected a string");
    if (hasLoneSurrogate(value)) fail(`${path}/text`, "invalid_text", "contains a lone surrogate");
    for (let i = 0; i < value.length; i++) {
      const o = value.charCodeAt(i);
      if ((o < 0x20 && o !== 9) || (o >= 0x7f && o <= 0x9f) || o === 0x2028 || o === 0x2029) {
        fail(`${path}/text`, "invalid_text", "contains a newline or control character (use hard_break for line breaks)");
      }
    }
    this.textCp += codepoints(value).length;
    if (this.textCp > this.limits.maxTextCodepoints) {
      fail(`${path}/text`, "limit_exceeded", `more than ${this.limits.maxTextCodepoints} characters of text`);
    }
    const marks = this.marks(node["marks"], `${path}/marks`);
    if (value === "") return null;
    const out: TextNode = { type: "text", text: value };
    if (Object.keys(marks).length > 0) out.marks = marks;
    return out;
  }

  marks(raw: unknown, path: string): Marks {
    if (raw === undefined || raw === null) return {};
    const m = this.obj(raw, path, [], ["bold", "italic", "underline", "strike", "script", "font", "size_pt", "color", "highlight", "link"]);
    const out: Marks = {};
    for (const key of ["bold", "italic", "underline", "strike"] as const) {
      if (has(m, key)) {
        if (typeof m[key] !== "boolean") fail(`${path}/${key}`, "invalid_type", "expected a boolean");
        if (m[key]) out[key] = true;
      }
    }
    if (has(m, "script")) {
      if (!(SCRIPTS as readonly unknown[]).includes(m["script"])) fail(`${path}/script`, "invalid_value", "must be superscript or subscript");
      out.script = m["script"] as "superscript" | "subscript";
    }
    if (has(m, "font")) {
      if (typeof m["font"] !== "string" || !this.limits.allowedFonts.includes(m["font"])) fail(`${path}/font`, "invalid_value", "not an allowed font key");
      out.font = m["font"];
    }
    if (has(m, "size_pt")) {
      const v = m["size_pt"];
      if (!isNumber(v) || v < 8 || v > 72 || !Number.isInteger(v * 2)) fail(`${path}/size_pt`, "invalid_value", "must be 8 to 72 in steps of 0.5");
      out.size_pt = v;
    }
    for (const key of ["color", "highlight"] as const) {
      if (has(m, key)) {
        const v = m[key];
        if (typeof v !== "string" || !COLOR_RE.test(v)) fail(`${path}/${key}`, "invalid_value", "must look like #RRGGBB");
        out[key] = v.toUpperCase();
      }
    }
    if (has(m, "link")) {
      const link = this.obj(m["link"], `${path}/link`, ["href"], []);
      if (typeof link["href"] !== "string") fail(`${path}/link/href`, "invalid_type", "expected a string");
      const clean = checkLink(link["href"], this.limits.maxLinkLength);
      if (clean === null) fail(`${path}/link/href`, "invalid_link", "only http, https and mailto links are allowed");
      out.link = { href: clean };
    }
    return out;
  }

  image(node: Obj, path: string): ImageNode {
    this.obj(node, path, ["type", "id", "asset_id", "width_px", "height_px"], ["alt"]);
    this.images++;
    if (this.images > this.limits.maxImages) fail(path, "limit_exceeded", `more than ${this.limits.maxImages} images`);
    const id = this.uid(node["id"], `${path}/id`);
    const asset = node["asset_id"];
    if (typeof asset !== "string" || !UUID_RE.test(asset)) fail(`${path}/asset_id`, "invalid_id", "must be a UUID");
    const assetId = (asset as string).toLowerCase();
    this.assetIds.add(assetId);
    const dims: Record<string, number> = {};
    for (const key of ["width_px", "height_px"]) {
      const v = node[key];
      if (!isInteger(v) || v < 1 || v > MAX_IMAGE_PX) fail(`${path}/${key}`, "invalid_value", `must be an integer 1 to ${MAX_IMAGE_PX}`);
      dims[key] = v;
    }
    const out: ImageNode = { type: "image", id, asset_id: assetId, width_px: dims["width_px"]!, height_px: dims["height_px"]! };
    if (has(node, "alt")) {
      const alt = node["alt"];
      if (typeof alt !== "string") fail(`${path}/alt`, "invalid_type", "expected a string");
      let bad = hasLoneSurrogate(alt);
      for (let i = 0; i < alt.length && !bad; i++) {
        const o = alt.charCodeAt(i);
        bad = o < 0x20 || (o >= 0x7f && o <= 0x9f) || o === 0x2028 || o === 0x2029;
      }
      if (bad) fail(`${path}/alt`, "invalid_text", "contains a control character");
      if (codepoints(alt).length > MAX_ALT_CODEPOINTS) fail(`${path}/alt`, "limit_exceeded", `at most ${MAX_ALT_CODEPOINTS} characters`);
      if (alt) out.alt = alt;
    }
    return out;
  }
}

function sameMarks(a: Marks | undefined, b: Marks | undefined): boolean {
  return canonicalJson((a ?? {}) as Json) === canonicalJson((b ?? {}) as Json);
}

// ---- canonical serialization ----------------------------------------------------------------------------------

/** Keys sorted, no whitespace. JSON.stringify writes integral numbers as integers and does not
 * \u-escape non-ASCII text, matching the Python twin byte for byte. */
export function canonicalJson(value: Json): string {
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (value !== null && typeof value === "object") {
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map((k) => JSON.stringify(k) + ":" + canonicalJson(value[k] as Json))
        .join(",") +
      "}"
    );
  }
  return JSON.stringify(value);
}

export function utf8Length(s: string): number {
  return new TextEncoder().encode(s).length;
}

/** Validate `raw` (parsed JSON) and return its canonical form, or throw InvalidDocument. */
export function validateDocument(raw: unknown, limits: Limits = DEFAULT_LIMITS): ValidatedDocument {
  const walk = new Walk(limits);
  const content = walk.root(raw);
  const bytes = utf8Length(canonicalJson(content as unknown as Json));
  if (bytes > limits.maxSerializedBytes) fail("", "limit_exceeded", `serialized content is larger than ${limits.maxSerializedBytes} bytes`);
  return {
    content,
    assetIds: walk.assetIds,
    stats: { text_codepoints: walk.textCp, nodes: walk.nodes, blocks: walk.blocks, inline_nodes: walk.inline, images: walk.images, bytes },
  };
}

export function newId(): string {
  return crypto.randomUUID();
}
