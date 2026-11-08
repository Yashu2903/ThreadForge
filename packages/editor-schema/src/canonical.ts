/**
 * Canonical form of a ProseMirror JSON document: the ONE deterministic shape used for hashing,
 * comparison and storage of validated snapshots.
 *
 *  - attributes equal to null/undefined or to the schema default are removed (editor.getJSON() writes
 *    `textAlign: "left"` and `lineSpacing: null` everywhere; they carry no information);
 *  - UNKNOWN attributes are kept (so they show up as differences when the schema would drop them);
 *  - marks are ordered by the schema's mark order, then by name;
 *  - adjacent text nodes with identical marks are merged and empty text nodes removed;
 *  - object keys are sorted when stringified.
 */
import type { Schema } from "@tiptap/pm/model";
import { canonicalStringify } from "./json.js";
import { getEditorSchema } from "./schema.js";

export { canonicalStringify };

export interface MarkJson {
  type: string;
  attrs?: Record<string, unknown>;
}
export interface PMJson {
  type?: string;
  attrs?: Record<string, unknown>;
  content?: PMJson[];
  marks?: MarkJson[];
  text?: string;
}

const same = (a: unknown, b: unknown): boolean => canonicalStringify(a) === canonicalStringify(b);

function cleanAttrs(attrs: Record<string, unknown> | undefined, spec: Record<string, { default?: unknown }> | undefined): Record<string, unknown> | undefined {
  if (!attrs) return undefined;
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    const known = spec && Object.prototype.hasOwnProperty.call(spec, key);
    if (known && same(value, spec![key]!.default)) continue;
    out[key] = value;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

export function canonicalize(json: PMJson, schema: Schema = getEditorSchema()): PMJson {
  const markOrder = Object.keys(schema.marks);
  const rank = (name: string) => {
    const i = markOrder.indexOf(name);
    return i === -1 ? markOrder.length : i;
  };

  const canonMarks = (marks: MarkJson[] | undefined): MarkJson[] | undefined => {
    if (!marks || marks.length === 0) return undefined;
    const out = marks.map((m) => {
      const spec = schema.marks[m.type]?.spec.attrs as Record<string, { default?: unknown }> | undefined;
      const attrs = cleanAttrs(m.attrs, spec);
      return attrs ? { type: m.type, attrs } : { type: m.type };
    });
    out.sort((a, b) => rank(a.type) - rank(b.type) || a.type.localeCompare(b.type));
    return out;
  };

  const canonNode = (node: PMJson): PMJson => {
    const out: PMJson = {};
    if (node.type !== undefined) out.type = node.type;
    if (node.type === "text") {
      out.text = node.text ?? "";
      const marks = canonMarks(node.marks);
      if (marks) out.marks = marks;
      return out;
    }
    const spec = node.type ? (schema.nodes[node.type]?.spec.attrs as Record<string, { default?: unknown }> | undefined) : undefined;
    const attrs = cleanAttrs(node.attrs, spec);
    if (attrs) out.attrs = attrs;
    if (node.content) {
      const merged: PMJson[] = [];
      for (const child of node.content.map(canonNode)) {
        if (child.type === "text" && child.text === "") continue;
        const last = merged[merged.length - 1];
        if (child.type === "text" && last && last.type === "text" && same(last.marks, child.marks)) {
          merged[merged.length - 1] = { ...last, text: (last.text ?? "") + (child.text ?? "") };
        } else merged.push(child);
      }
      if (merged.length > 0) out.content = merged;
    }
    const marks = canonMarks(node.marks);
    if (marks) out.marks = marks;
    return out;
  };

  return canonNode(json);
}

/** The first path where `a` and `b` differ (JSON-pointer-like, over `content`/`attrs`/`marks`), or null. */
export function firstDifference(a: PMJson, b: PMJson, path = ""): string | null {
  if (a.type !== b.type) return path || "/";
  if (a.text !== b.text) return path || "/";
  if (!same(a.marks, b.marks)) return path + "/marks";
  const attrsA = a.attrs ?? {};
  const attrsB = b.attrs ?? {};
  for (const key of new Set([...Object.keys(attrsA), ...Object.keys(attrsB)])) {
    if (!same(attrsA[key], attrsB[key])) return `${path}/attrs/${key}`;
  }
  const ca = a.content ?? [];
  const cb = b.content ?? [];
  const n = Math.max(ca.length, cb.length);
  for (let i = 0; i < n; i++) {
    if (!ca[i] || !cb[i]) return `${path}/content/${i}`;
    const d = firstDifference(ca[i]!, cb[i]!, `${path}/content/${i}`);
    if (d) return d;
  }
  return null;
}
