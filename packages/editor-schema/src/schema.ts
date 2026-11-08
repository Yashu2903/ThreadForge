import { getSchema } from "@tiptap/core";
import { canonicalStringify } from "./json.js";
import type { Schema } from "@tiptap/pm/model";
import { createExtensions, type CreateExtensionsOptions } from "./extensions.js";

/**
 * The name of the Yjs XmlFragment that holds the document. Tiptap's Collaboration extension uses
 * "default" unless told otherwise; the browser (Collaboration `field`) and the collaboration service
 * (seeding and snapshots) both import this constant so they can never disagree.
 */
export const YJS_FIELD = "default";

let cached: Schema | null = null;

/** The ProseMirror schema built from createExtensions(). Cached for the default (no options) case. */
export function getEditorSchema(options?: CreateExtensionsOptions): Schema {
  if (options) return getSchema(createExtensions(options));
  cached ??= getSchema(createExtensions());
  return cached;
}

/**
 * A deterministic description of the schema: every node and mark with its content expression, group,
 * excludes and attributes (name and default). The browser editor and the collaboration service both
 * import this package; a test on each side compares this value with the pinned one, so a stale build
 * or a second, divergent definition is caught immediately instead of silently dropping content.
 */
export function schemaDescription(schema: Schema = getEditorSchema()): unknown {
  const attrs = (spec: { attrs?: Record<string, { default?: unknown }> }) =>
    Object.fromEntries(Object.entries(spec.attrs ?? {}).map(([k, v]) => [k, v.default ?? null]));
  return {
    nodes: Object.fromEntries(
      Object.entries(schema.nodes).map(([name, t]) => [
        name,
        { content: t.spec.content ?? null, group: t.spec.group ?? null, inline: !!t.spec.inline, atom: !!t.spec.atom, attrs: attrs(t.spec as never) },
      ]),
    ),
    marks: Object.fromEntries(
      Object.entries(schema.marks).map(([name, t]) => [name, { excludes: t.spec.excludes ?? null, attrs: attrs(t.spec as never) }]),
    ),
    markOrder: Object.keys(schema.marks),
  };
}

export async function schemaFingerprint(schema: Schema = getEditorSchema()): Promise<string> {
  const text = canonicalStringify(schemaDescription(schema));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}
