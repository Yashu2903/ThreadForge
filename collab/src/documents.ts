/**
 * Pure document helpers: turning a seed into a Yjs document, and a Yjs document into a validated
 * snapshot. No network, no server state, so they are unit-testable on their own.
 */
import {
  AdaptError,
  YJS_FIELD,
  adapt,
  canonicalize,
  contentHash,
  extractText,
  getEditorSchema,
  validateDocument,
  type DocError,
  type Limits,
  type PMJson,
} from "@threadforge/editor-schema";
import { prosemirrorJSONToYDoc, yDocToProsemirrorJSON } from "@tiptap/y-tiptap";
import * as Y from "yjs";

export const DOCUMENT_NAME = /^doc:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;

/** "doc:<uuid>" to the uuid, or null for anything else. */
export function documentIdOf(documentName: string): string | null {
  return DOCUMENT_NAME.exec(documentName)?.[1] ?? null;
}

/** The seed could not be turned into a valid document. The reason is data, never content. */
export class SeedError extends Error {
  constructor(
    readonly reason: "adapt_failed" | "invalid",
    readonly detail: string,
  ) {
    super(`${reason}: ${detail}`);
    this.name = "SeedError";
  }
}

export interface SeedDocument {
  ydoc: Y.Doc;
  json: PMJson;
}

/**
 * interchange JSON (or an operator-restored Tiptap snapshot) to a Yjs document.
 * adapt() fails loudly on anything unmappable; validateDocument() then applies the same rules every
 * snapshot is held to, so a document that cannot be snapshotted can never be seeded.
 */
export function buildSeed(format: "interchange" | "tiptap", content: unknown, limits: Limits): SeedDocument {
  let json: PMJson;
  try {
    json = format === "interchange" ? adapt(content) : canonicalize(content as PMJson);
  } catch (error) {
    if (error instanceof AdaptError) throw new SeedError("adapt_failed", error.message);
    throw new SeedError("adapt_failed", "unreadable seed");
  }
  const result = validateDocument(json, limits);
  if (!result.ok) {
    const first = result.errors[0];
    throw new SeedError("invalid", `${first?.code ?? "invalid"} at ${first?.path ?? "/"}`);
  }
  return { ydoc: prosemirrorJSONToYDoc(getEditorSchema(), result.doc, YJS_FIELD), json: result.doc };
}

export interface SnapshotData {
  content: PMJson;
  contentHash: string;
  textLength: number;
}

export type SnapshotOutcome = { ok: true; data: SnapshotData } | { ok: false; errors: DocError[] };

/** The document's plain JSON, validated against the editor schema and limits. */
export async function validatedSnapshot(ydoc: Y.Doc, limits: Limits): Promise<SnapshotOutcome> {
  let json: PMJson;
  try {
    json = yDocToProsemirrorJSON(ydoc, YJS_FIELD) as PMJson;
  } catch {
    // the Yjs tree cannot even be read with this schema (an unknown node type, for instance)
    return { ok: false, errors: [{ path: "", code: "unreadable", message: "the document cannot be read with the editor schema" }] };
  }
  // "structure": a person's half-typed diagram source must not make a snapshot fail (see DiagramPolicy)
  const result = validateDocument(canonicalize(json), limits, getEditorSchema(), "structure");
  if (!result.ok) return { ok: false, errors: result.errors };
  return {
    ok: true,
    data: {
      content: result.doc,
      contentHash: await contentHash(result.doc),
      textLength: Array.from(extractText(result.doc)).length,
    },
  };
}

/** The same, from encoded Yjs state (the exact bytes that were stored under a given counter). */
export async function validatedSnapshotOfState(state: Uint8Array, limits: Limits): Promise<SnapshotOutcome> {
  const ydoc = new Y.Doc();
  try {
    Y.applyUpdate(ydoc, state);
    return await validatedSnapshot(ydoc, limits);
  } finally {
    ydoc.destroy();
  }
}
