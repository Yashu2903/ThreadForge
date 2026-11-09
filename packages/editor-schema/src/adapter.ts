/**
 * adapt(interchangeJson) -> Tiptap/ProseMirror JSON.
 *
 * The "interchange" format is the canonical JSON that templates and the DOCX converter produce and
 * that the Python validator checks (docs/document-schema.md). This is the single adapter that turns
 * it into the editor's schema. Unmappable input FAILS with an AdaptError carrying a path: nothing is
 * ever dropped silently, including unknown keys. The mapping table is in docs/collaboration.md.
 *
 * Differences that are deliberate and listed there:
 *  - interchange image ids are not kept (an inline image has no id in the editor schema);
 *  - interchange block ids ARE kept as the `id` attribute;
 *  - attribute defaults are omitted from the output (canonical form).
 */
import { type MarkJson, type PMJson, canonicalize } from "./canonical.js";

export class AdaptError extends Error {
  constructor(
    readonly path: string,
    message: string,
  ) {
    super(`${path || "/"}: ${message}`);
    this.name = "AdaptError";
  }
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

function only(obj: Obj, allowed: string[], path: string): void {
  for (const key of Object.keys(obj)) if (!allowed.includes(key)) throw new AdaptError(`${path}/${key}`, "unknown field");
}
function need(obj: Obj, key: string, path: string): unknown {
  if (!(key in obj)) throw new AdaptError(`${path}/${key}`, "required field is missing");
  return obj[key];
}
function list(v: unknown, path: string): unknown[] {
  if (!Array.isArray(v)) throw new AdaptError(path, "expected an array");
  return v;
}

function blockAttrs(obj: Obj, path: string): Record<string, unknown> {
  const attrs: Record<string, unknown> = { id: need(obj, "id", path) };
  if (obj.attrs !== undefined) {
    if (!isObj(obj.attrs)) throw new AdaptError(`${path}/attrs`, "expected an object");
    const a = obj.attrs;
    only(a, ["align", "line_spacing", "space_before_pt", "space_after_pt", "indent_level"], `${path}/attrs`);
    if (a.align !== undefined) attrs.textAlign = a.align;
    if (a.line_spacing !== undefined) attrs.lineSpacing = a.line_spacing;
    if (a.space_before_pt !== undefined) attrs.spaceBefore = a.space_before_pt;
    if (a.space_after_pt !== undefined) attrs.spaceAfter = a.space_after_pt;
    if (a.indent_level !== undefined) attrs.indent = a.indent_level;
  }
  return attrs;
}

function marksOf(raw: unknown, path: string): MarkJson[] {
  if (raw === undefined) return [];
  if (!isObj(raw)) throw new AdaptError(path, "expected an object");
  only(raw, ["bold", "italic", "underline", "strike", "script", "font", "size_pt", "color", "highlight", "link"], path);
  const out: MarkJson[] = [];
  for (const key of ["bold", "italic", "underline", "strike"] as const) {
    if (raw[key] === true) out.push({ type: key });
    else if (raw[key] !== undefined && raw[key] !== false) throw new AdaptError(`${path}/${key}`, "expected a boolean");
  }
  if (raw.script !== undefined) {
    if (raw.script !== "superscript" && raw.script !== "subscript") throw new AdaptError(`${path}/script`, "must be superscript or subscript");
    out.push({ type: raw.script });
  }
  const style: Record<string, unknown> = {};
  if (raw.font !== undefined) style.fontKey = raw.font;
  if (raw.size_pt !== undefined) style.fontSizePt = raw.size_pt;
  if (raw.color !== undefined) style.color = raw.color;
  if (Object.keys(style).length > 0) out.push({ type: "textStyle", attrs: style });
  if (raw.highlight !== undefined) out.push({ type: "highlight", attrs: { color: raw.highlight } });
  if (raw.link !== undefined) {
    if (!isObj(raw.link)) throw new AdaptError(`${path}/link`, "expected an object");
    only(raw.link, ["href"], `${path}/link`);
    out.push({ type: "link", attrs: { href: need(raw.link, "href", `${path}/link`) } });
  }
  return out;
}

function inlines(raw: unknown, path: string): PMJson[] {
  if (raw === undefined) return [];
  return list(raw, path).flatMap((n, i): PMJson[] => {
    const p = `${path}/${i}`;
    if (!isObj(n)) throw new AdaptError(p, "expected an object");
    switch (n.type) {
      case "text": {
        only(n, ["type", "text", "marks"], p);
        const text = need(n, "text", p);
        if (typeof text !== "string") throw new AdaptError(`${p}/text`, "expected a string");
        if (text === "") return [];
        const marks = marksOf(n.marks, `${p}/marks`);
        return [marks.length > 0 ? { type: "text", text, marks } : { type: "text", text }];
      }
      case "hard_break":
        only(n, ["type"], p);
        return [{ type: "hardBreak" }];
      case "image": {
        only(n, ["type", "id", "asset_id", "alt", "width_px", "height_px"], p);
        return [
          {
            type: "image",
            attrs: { assetId: need(n, "asset_id", p), alt: n.alt ?? "", width: need(n, "width_px", p), height: need(n, "height_px", p) },
          },
        ];
      }
      default:
        throw new AdaptError(`${p}/type`, `cannot map inline type ${JSON.stringify(n.type)}`);
    }
  });
}

function textBlock(n: Obj, path: string): PMJson {
  const attrs = blockAttrs(n, path);
  if (n.type === "heading") {
    only(n, ["type", "id", "level", "attrs", "children"], path);
    attrs.level = need(n, "level", path);
  } else only(n, ["type", "id", "attrs", "children"], path);
  const content = inlines(n.children, `${path}/children`);
  return { type: n.type as string, attrs, ...(content.length > 0 ? { content } : {}) };
}

function paragraphs(raw: unknown, path: string): PMJson[] {
  return list(raw, path).map((c, i) => {
    if (!isObj(c) || c.type !== "paragraph") throw new AdaptError(`${path}/${i}`, "expected a paragraph");
    return textBlock(c, `${path}/${i}`);
  });
}

function block(n: unknown, path: string): PMJson {
  if (!isObj(n)) throw new AdaptError(path, "expected an object");
  switch (n.type) {
    case "paragraph":
    case "heading":
      return textBlock(n, path);
    case "quote":
      only(n, ["type", "id", "children"], path);
      return { type: "blockquote", attrs: { id: need(n, "id", path) }, content: paragraphs(need(n, "children", path), `${path}/children`) };
    case "list": {
      only(n, ["type", "id", "ordered", "children"], path);
      const items = list(need(n, "children", path), `${path}/children`).map((item, i) => {
        const ip = `${path}/children/${i}`;
        if (!isObj(item) || item.type !== "list_item") throw new AdaptError(ip, "expected a list item");
        only(item, ["type", "id", "children"], ip);
        return {
          type: "listItem",
          attrs: { id: need(item, "id", ip) },
          content: list(need(item, "children", ip), `${ip}/children`).map((c, j) => block(c, `${ip}/children/${j}`)),
        };
      });
      return { type: n.ordered === true ? "orderedList" : "bulletList", attrs: { id: need(n, "id", path) }, content: items };
    }
    case "table": {
      only(n, ["type", "id", "children"], path);
      const rows = list(need(n, "children", path), `${path}/children`).map((row, r) => {
        const rp = `${path}/children/${r}`;
        if (!isObj(row) || row.type !== "table_row") throw new AdaptError(rp, "expected a table row");
        only(row, ["type", "id", "children"], rp);
        const cells = list(need(row, "children", rp), `${rp}/children`).map((cell, c) => {
          const cp = `${rp}/children/${c}`;
          if (!isObj(cell) || cell.type !== "table_cell") throw new AdaptError(cp, "expected a table cell");
          only(cell, ["type", "id", "header", "children"], cp);
          return {
            type: cell.header === true ? "tableHeader" : "tableCell",
            attrs: { id: need(cell, "id", cp) },
            content: paragraphs(need(cell, "children", cp), `${cp}/children`),
          };
        });
        return { type: "tableRow", attrs: { id: need(row, "id", rp) }, content: cells };
      });
      return { type: "table", attrs: { id: need(n, "id", path) }, content: rows };
    }
    case "diagram": {
      // Not part of interchange schema_version 1 (the Python validator does not know it). Accepted here so
      // one shape can be used by tests and tools; documents.seed_format "tiptap" is the route AI drafts take.
      only(n, ["type", "id", "source"], path);
      const source = need(n, "source", path);
      if (typeof source !== "string") throw new AdaptError(`${path}/source`, "expected a string");
      return { type: "diagram", attrs: { id: need(n, "id", path) }, ...(source ? { content: [{ type: "text", text: source }] } : {}) };
    }
    default:
      throw new AdaptError(`${path}/type`, `cannot map block type ${JSON.stringify(n.type)}`);
  }
}

export function adapt(interchange: unknown): PMJson {
  if (!isObj(interchange)) throw new AdaptError("", "expected an object");
  only(interchange, ["schema_version", "blocks"], "");
  if (interchange.schema_version !== 1) throw new AdaptError("/schema_version", "only schema_version 1 is supported");
  const blocks = list(need(interchange, "blocks", ""), "/blocks");
  if (blocks.length === 0) throw new AdaptError("/blocks", "a document needs at least one block");
  return canonicalize({ type: "doc", content: blocks.map((b, i) => block(b, `/blocks/${i}`)) });
}
