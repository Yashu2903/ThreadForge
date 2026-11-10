/**
 * The proposal check, as a pure function (the CLI in cli.ts is a thin stdin/stdout wrapper around it).
 *
 *   request  {mode: "edit",  content, operations, scope}   build the candidate document of an edit proposal
 *            {mode: "draft", blocks}                        build a whole new document from a draft's blocks
 *   response {ok: true,  doc?, changes, inserted_chars, hashes}
 *            {ok: false, errors: [{op, code, message, path?}]}
 *
 * `content` is the captured document (validated ProseMirror JSON). `scope` is what the request declared
 * (see backend/app/ai/edit/target.py). Everything here is the SAME code the collaboration service runs when a
 * person applies the proposal, so "valid here" and "valid at apply" cannot drift apart.
 */
import { randomUUID } from "node:crypto";
import {
  type OpError,
  type OpScope,
  type PMJson,
  applyOperations,
  blockHash,
  buildDraftDocument,
  validateDocument,
} from "@threadforge/editor-schema";

export interface CheckRequest {
  mode: "edit" | "draft";
  content?: PMJson;
  operations?: unknown;
  blocks?: unknown;
  scope?: { whole_ids?: string[]; ranges?: { block_id: string; start: number; end: number }[]; anchors?: string[] };
}

export type CheckResponse =
  | { ok: true; doc: PMJson | null; changes: unknown[]; inserted_chars: number; hashes: Record<string, string> }
  | { ok: false; errors: OpError[] };

const fail = (code: string, message: string): CheckResponse => ({ ok: false, errors: [{ op: null, code, message }] });

export async function check(request: CheckRequest): Promise<CheckResponse> {
  const newId = () => randomUUID();
  if (request.mode === "draft") {
    const built = buildDraftDocument({ title: "", blocks: request.blocks as never }, { newId });
    if (!built.ok) return built;
    return { ok: true, doc: built.doc, changes: [], inserted_chars: built.insertedChars, hashes: {} };
  }
  if (request.mode !== "edit" || !request.content || request.content.type !== "doc") return fail("invalid_request", "an edit check needs the document");
  // the captured document must itself be a document the editor accepts (a capture is never trusted blindly);
  // "structure": a half-typed diagram elsewhere in it is not this proposal's problem
  const base = validateDocument(request.content, undefined, undefined, "structure");
  if (!base.ok) return fail("document_invalid", base.errors[0]?.message ?? "the document is not valid");
  const scope: OpScope = {
    wholeIds: new Set(request.scope?.whole_ids ?? []),
    ranges: request.scope?.ranges ?? [],
    anchors: request.scope?.anchors ?? [],
  };
  const result = applyOperations(base.doc, request.operations, { scope, newId });
  if (!result.ok) return result;
  // hashes of the blocks the proposal produced, so a preview can show "this replaces hash A with hash B"
  const hashes: Record<string, string> = {};
  for (const change of result.changes) {
    for (const node of change.after) {
      const id = node.attrs?.id;
      if (typeof id === "string") hashes[id] = await blockHash(node);
    }
  }
  return { ok: true, doc: null, changes: result.changes, inserted_chars: result.insertedChars, hashes };
}
