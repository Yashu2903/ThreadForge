/**
 * The five document templates go through the real seeding path (interchange JSON -> adapt -> validate ->
 * Yjs -> validated snapshot), exactly like any legacy document.
 */
import { execFileSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";
import { BACKEND_DIR } from "./env";
import { all, createDocument, scene } from "./fixtures";
import { connect, docText, startCollab, stopAll, until } from "./harness";

afterEach(stopAll);

const SCRIPT = [
  "import json",
  "from app.documents.schema import DEFAULT_LIMITS",
  "from app.documents.templates import template_document",
  "keys = ['blank', 'project_brief', 'technical_proposal', 'meeting_notes', 'research_summary']",
  "print(json.dumps({k: template_document(k, DEFAULT_LIMITS).content for k in keys}))",
].join("\n");

const templates = JSON.parse(execFileSync("uv", ["run", "python", "-c", SCRIPT], { cwd: BACKEND_DIR, encoding: "utf-8" }).trim().split("\n").pop()!) as Record<string, unknown>;

describe("templates", () => {
  it("all five seed into a valid collaborative document with a seed snapshot", async () => {
    expect(Object.keys(templates)).toHaveLength(5);
    const server = await startCollab();
    for (const [key, content] of Object.entries(templates)) {
      const s = await scene();
      const docId = await createDocument(s.roomId, s.owner, content, key);
      const client = connect(server, s.owner, docId);
      await client.ready;
      expect(docText(client.ydoc).length, key).toBeGreaterThanOrEqual(0);
      const snaps = await all<{ kind: string }>("SELECT kind FROM document_snapshots WHERE document_id = $1", [docId]);
      expect(snaps.map((r) => r.kind), key).toContain("seed");
      const state = await all<{ collab_state: string }>("SELECT collab_state FROM documents WHERE id = $1", [docId]);
      expect(state[0]!.collab_state, key).toBe("ready");
    }
  });

  it("a template's headings arrive as real headings, and blocks have stable ids", async () => {
    const server = await startCollab();
    const s = await scene();
    const docId = await createDocument(s.roomId, s.owner, templates.project_brief, "brief");
    const client = connect(server, s.owner, docId);
    await client.ready;
    await until(async () => (await all("SELECT 1 FROM document_snapshots WHERE document_id = $1", [docId])).length > 0, "seed snapshot");
    const [snap] = await all<{ content: { content: { type: string; attrs?: { id?: string } }[] } }>(
      "SELECT content FROM document_snapshots WHERE document_id = $1 ORDER BY created_at LIMIT 1",
      [docId],
    );
    const blocks = snap!.content.content;
    expect(blocks.some((b) => b.type === "heading")).toBe(true);
    const ids = blocks.map((b) => b.attrs?.id);
    expect(ids.every((id) => typeof id === "string")).toBe(true);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
