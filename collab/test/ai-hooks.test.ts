/**
 * The AI-ready hooks (read and change-a-block). There is no AI here: the tests call the hooks the way a
 * later feature would. Real FastAPI + PostgreSQL + WebSocket editors; only the caller is a test.
 */
import { contentHash, blockHash } from "@threadforge/editor-schema";
import { afterEach, describe, expect, it } from "vitest";
import { INTERNAL_SECRET } from "./env";
import { all, createUser, interchange, one, pool, addMember, scene, createDocument } from "./fixtures";
import { appendText, connect, docJson, docText, sleep, startCollab, stopAll, until } from "./harness";
import { createLogger } from "../src/log";

afterEach(stopAll);

type Collab = Awaited<ReturnType<typeof startCollab>>;

async function call(server: Collab, path: "read" | "apply", body: unknown, secret: string | null = INTERNAL_SECRET) {
  const response = await fetch(`http://127.0.0.1:${server.port}/internal/ai/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(secret ? { "X-Internal-Secret": secret } : {}) },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  return { status: response.status, body: (await response.json()) as any };
}

const counterInDb = async (docId: string) => Number((await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [docId])).update_counter);
const paragraph = (text: string, attrs: Record<string, unknown> = {}) => ({ type: "paragraph", attrs, content: [{ type: "text", text }] });

async function readBlocks(server: Collab, docId: string, requester: string) {
  const r = await call(server, "read", { document_id: docId, requester_id: requester });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body as { yjs_update_counter: number; content_hash: string; content: any; blocks: { id: string; type: string; hash: string; parent_id: string | null; text: string }[] };
}

describe("AI hook: read", () => {
  it("returns the stored state exactly: counter, content hash, per-block hashes, text", async () => {
    const s = await scene(interchange("First", "Second", "Third"));
    const server = await startCollab();
    const read = await readBlocks(server, s.docId, s.owner.id);
    expect(read.blocks.map((b) => [b.type, b.text])).toEqual([["paragraph", "First"], ["paragraph", "Second"], ["paragraph", "Third"]]);
    expect(read.blocks.every((b) => /^[0-9a-f]{64}$/.test(b.hash) && b.parent_id === null)).toBe(true);
    expect(read.content_hash).toBe(await contentHash(read.content));
    for (const block of read.blocks) {
      const node = read.content.content.find((n: any) => n.attrs.id === block.id);
      expect(block.hash).toBe(await blockHash(node)); // the hash the apply hook will demand
    }
    // the counter is the one in the database: a caller can say precisely which state it saw
    expect(read.yjs_update_counter).toBe(await counterInDb(s.docId));
  });

  it("includes edits that are still waiting for the debounce, and the counter matches what was stored", async () => {
    const s = await scene(interchange("Base"));
    const server = await startCollab({ debounceMs: 60_000, maxDebounceMs: 120_000 }); // nothing would be stored by itself
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    appendText(editor.ydoc, " + live edit");
    await until(() => true);
    await sleep(200);
    const read = await readBlocks(server, s.docId, s.owner.id);
    expect(read.blocks[0]!.text).toBe("Base + live edit");
    expect(read.yjs_update_counter).toBe(await counterInDb(s.docId));
    const again = await readBlocks(server, s.docId, s.owner.id);
    expect(again.yjs_update_counter).toBe(read.yjs_update_counter); // nothing changed: nothing stored again
  });

  it("works on a legacy document (it is seeded first)", async () => {
    const s = await scene(interchange("Legacy text"));
    const server = await startCollab();
    expect((await readBlocks(server, s.docId, s.owner.id)).blocks[0]!.text).toBe("Legacy text");
    expect((await one<{ collab_state: string }>("SELECT collab_state FROM documents WHERE id = $1", [s.docId])).collab_state).toBe("ready");
  });

  it("refuses requesters who may not write, and bad requests; needs the secret", async () => {
    const s = await scene(interchange("Secret plan"));
    const stranger = await createUser("Stranger");
    const server = await startCollab();
    for (const who of [s.viewer, stranger]) {
      const r = await call(server, "read", { document_id: s.docId, requester_id: who.id });
      expect(r.status).toBe(403);
      expect(JSON.stringify(r.body)).not.toContain("Secret plan");
    }
    await pool.query("UPDATE documents SET collab_state = 'needs_repair' WHERE id = $1", [s.docId]);
    expect((await call(server, "read", { document_id: s.docId, requester_id: s.owner.id })).body.reason).toBe("needs_repair");
    expect((await call(server, "read", { document_id: "x", requester_id: s.owner.id })).status).toBe(422);
    expect((await call(server, "read", "not json")).status).toBe(422);
    expect((await call(server, "read", { document_id: s.docId, requester_id: s.owner.id }, null)).status).toBe(401);
    expect((await call(server, "read", { document_id: s.docId, requester_id: s.owner.id }, "wrong-secret")).status).toBe(401);
  });
});

describe("AI hook: apply a change to one block", () => {
  it("replaces exactly that block, keeps its id, merges with collaborators, and uses its own transaction origin", async () => {
    const s = await scene(interchange("One", "Two", "Three"));
    const server = await startCollab();
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    const read = await readBlocks(server, s.docId, s.owner.id);
    const target = read.blocks[1]!;
    const origins: unknown[] = [];
    server.hocuspocus.documents.get(`doc:${s.docId}`)!.on("afterTransaction", (tr: { origin: unknown }) => origins.push(tr.origin));

    // a collaborator types in ANOTHER block at the same time
    appendText(editor.ydoc, " (edited by a person)");
    const result = await call(server, "apply", {
      document_id: s.docId,
      requester_id: s.owner.id,
      block_id: target.id,
      base_hash: target.hash,
      block: paragraph("Two, rewritten"),
    });
    expect(result.status, JSON.stringify(result.body)).toBe(200);
    expect(result.body.yjs_update_counter).toBe(await counterInDb(s.docId));

    await until(() => docText(editor.ydoc) === "One (edited by a person)\nTwo, rewritten\nThree", "the editor sees both changes");
    const json = docJson(editor.ydoc);
    expect(json.content!.map((n) => n.attrs!.id)).toEqual(read.blocks.map((b) => b.id)); // ids are stable
    // the change is distinguishable from every browser edit
    const ai = origins.filter((o: any) => o?.context?.source === "ai-hook");
    expect(ai).toHaveLength(1);
    expect((ai[0] as any).source).toBe("local");
    expect((ai[0] as any).context.requester_id).toBe(s.owner.id);
    // and the reply carries what a follow-up needs
    const after = await readBlocks(server, s.docId, s.owner.id);
    expect(after.blocks[1]!.hash).toBe(result.body.block_hash);
    expect(after.content_hash).toBe(result.body.content_hash);
  });

  it("refuses a change based on an old version of the block (409) and changes nothing", async () => {
    const s = await scene(interchange("Keep me", "Other"));
    const server = await startCollab();
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    const read = await readBlocks(server, s.docId, s.owner.id);
    appendText(editor.ydoc, " - changed meanwhile"); // the first block changes after the caller read it
    await sleep(300);
    const r = await call(server, "apply", {
      document_id: s.docId,
      requester_id: s.owner.id,
      block_id: read.blocks[0]!.id,
      base_hash: read.blocks[0]!.hash,
      block: paragraph("AI text that must not land"),
    });
    expect(r.status).toBe(409);
    expect(r.body.error).toBe("block_changed");
    expect(r.body.current_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(JSON.stringify(r.body)).not.toContain("changed meanwhile"); // no content in the refusal
    expect(docText(editor.ydoc)).toBe("Keep me - changed meanwhile\nOther");
  });

  it("refuses an unknown block (404) and a result that breaks the schema (422), changing nothing", async () => {
    const s = await scene(interchange("Alpha", "Beta"));
    const server = await startCollab();
    const read = await readBlocks(server, s.docId, s.owner.id);
    const base = { document_id: s.docId, requester_id: s.owner.id, block_id: read.blocks[0]!.id, base_hash: read.blocks[0]!.hash };
    expect((await call(server, "apply", { ...base, block_id: "11111111-1111-4111-8111-111111111111", block: paragraph("x") })).status).toBe(404);
    const bad = [
      { type: "alien", attrs: {} }, // unknown node type
      { type: "heading", attrs: { level: 9 }, content: [{ type: "text", text: "x" }] },
      { type: "paragraph", content: [{ type: "text", text: "x", marks: [{ type: "link", attrs: { href: "javascript:alert(1)" } }] }] },
      { type: "paragraph", content: [{ type: "image", attrs: { assetId: "11111111-1111-4111-8111-111111111111" } }] }, // an asset that does not exist is caught at snapshot; this one has no size either
    ];
    for (const block of bad.slice(0, 3)) {
      const r = await call(server, "apply", { ...base, block });
      expect(r.status, JSON.stringify(block)).toBe(422);
      expect(r.body.error).toBe("invalid_change");
      expect(r.body.path).toBeTypeOf("string");
    }
    const after = await readBlocks(server, s.docId, s.owner.id);
    expect(after.blocks.map((b) => b.text)).toEqual(["Alpha", "Beta"]);
    expect(after.yjs_update_counter).toBe(read.yjs_update_counter); // not even a store happened
  });

  it("checks the requester on EVERY call: a viewer, a removed member and a stranger cannot change anything", async () => {
    const s = await scene(interchange("Protected"));
    const stranger = await createUser("Stranger");
    const server = await startCollab();
    const read = await readBlocks(server, s.docId, s.owner.id);
    const body = (who: string) => ({ document_id: s.docId, requester_id: who, block_id: read.blocks[0]!.id, base_hash: read.blocks[0]!.hash, block: paragraph("vandalism") });
    for (const who of [s.viewer.id, stranger.id]) expect((await call(server, "apply", body(who))).status).toBe(403);
    // allowed while an editor ...
    await addMember(s.roomId, s.editor, "editor");
    const ok = await call(server, "apply", { ...body(s.editor.id), block: paragraph("by the editor") });
    expect(ok.status).toBe(200);
    // ... and refused the moment the role is gone, with no restart and no cache
    await pool.query("UPDATE memberships SET state = 'removed', removed_at = now() WHERE room_id = $1 AND user_id = $2", [s.roomId, s.editor.id]);
    const fresh = await readBlocks(server, s.docId, s.owner.id);
    const refused = await call(server, "apply", { ...body(s.editor.id), base_hash: fresh.blocks[0]!.hash });
    expect(refused.status).toBe(403);
    expect((await readBlocks(server, s.docId, s.owner.id)).blocks[0]!.text).toBe("by the editor");
  });

  it("limits size and frequency", async () => {
    const s = await scene(interchange("Limits"));
    const server = await startCollab({ aiAppliesPerMinute: 3, aiMaxBlockBytes: 2_000 });
    const read = await readBlocks(server, s.docId, s.owner.id);
    const base = { document_id: s.docId, requester_id: s.owner.id, block_id: read.blocks[0]!.id, base_hash: read.blocks[0]!.hash };
    expect((await call(server, "apply", { ...base, block: paragraph("x".repeat(3_000)) })).status).toBe(413);
    // body limit: refused before parsing
    const huge = await call(server, "apply", { ...base, block: paragraph("y".repeat(300_000)) });
    expect(huge.status).toBe(413);
    // frequency: the 4th attempt within a minute is refused (valid or not, every attempt counts)
    const statuses = [];
    for (let i = 0; i < 4; i += 1) statuses.push((await call(server, "apply", { ...base, base_hash: "0".repeat(64), block: paragraph("a") })).status);
    expect(statuses).toEqual([409, 409, 409, 429]);
  });

  it("never logs document text or the new block", async () => {
    const s = await scene(interchange("PRIVATE-ORIGINAL-TEXT"));
    const lines: string[] = [];
    const server = await startCollab({ logLevel: "debug" }, { logger: createLogger("debug", (line) => lines.push(line)) });
    const read = await readBlocks(server, s.docId, s.owner.id);
    await call(server, "apply", { document_id: s.docId, requester_id: s.owner.id, block_id: read.blocks[0]!.id, base_hash: read.blocks[0]!.hash, block: paragraph("PRIVATE-NEW-TEXT") });
    await call(server, "apply", { document_id: s.docId, requester_id: s.owner.id, block_id: read.blocks[0]!.id, base_hash: "f".repeat(64), block: paragraph("PRIVATE-REFUSED-TEXT") });
    const log = lines.join("\n");
    expect(log).toContain("ai apply"); // it did log
    for (const secret of ["PRIVATE-ORIGINAL-TEXT", "PRIVATE-NEW-TEXT", "PRIVATE-REFUSED-TEXT", INTERNAL_SECRET]) expect(log).not.toContain(secret);
  });

  it("the change is persisted: stored state and the next validated snapshot contain it", async () => {
    const s = await scene(interchange("Before"));
    const seedBefore = (await one<{ content: unknown }>("SELECT content FROM documents WHERE id = $1", [s.docId])).content;
    const server = await startCollab({ snapshotIntervalMs: 200 });
    const read = await readBlocks(server, s.docId, s.owner.id);
    const r = await call(server, "apply", { document_id: s.docId, requester_id: s.owner.id, block_id: read.blocks[0]!.id, base_hash: read.blocks[0]!.hash, block: paragraph("After") });
    expect(r.status).toBe(200);
    await until(async () => (await all("SELECT 1 FROM document_snapshots WHERE document_id = $1 AND content::text LIKE '%After%'", [s.docId])).length > 0, "snapshot with the change", 8_000);
    expect((await one<{ collab_state: string }>("SELECT collab_state FROM documents WHERE id = $1", [s.docId])).collab_state).toBe("ready");
    expect((await one<{ content: unknown }>("SELECT content FROM documents WHERE id = $1", [s.docId])).content).toEqual(seedBefore); // seed untouched
  });

  it("can replace a nested block (a list item) by id; missing ids in the new content are generated", async () => {
    const content = {
      schema_version: 1,
      blocks: [
        { type: "list", id: "aaaaaaaa-0000-4000-8000-000000000001", ordered: false, children: [
          { type: "list_item", id: "aaaaaaaa-0000-4000-8000-000000000002", children: [{ type: "paragraph", id: "aaaaaaaa-0000-4000-8000-000000000003", children: [{ type: "text", text: "item one" }] }] },
          { type: "list_item", id: "aaaaaaaa-0000-4000-8000-000000000004", children: [{ type: "paragraph", id: "aaaaaaaa-0000-4000-8000-000000000005", children: [{ type: "text", text: "item two" }] }] },
        ] },
      ],
    };
    const owner = await createUser("Nested");
    const roomId = (await pool.query("INSERT INTO rooms (name, owner_id) VALUES ('R', $1) RETURNING id", [owner.id])).rows[0].id;
    await addMember(roomId, owner, "owner");
    const docId = await createDocument(roomId, owner, content, "Nested");
    const server = await startCollab();
    const read = await readBlocks(server, docId, owner.id);
    const item = read.blocks.find((b) => b.type === "listItem" && b.text === "item two")!;
    expect(item.parent_id).not.toBeNull();
    const r = await call(server, "apply", {
      document_id: docId,
      requester_id: owner.id,
      block_id: item.id,
      base_hash: item.hash,
      block: { type: "listItem", content: [paragraph("item two, improved")] }, // no ids given
    });
    expect(r.status, JSON.stringify(r.body)).toBe(200);
    const after = await readBlocks(server, docId, owner.id);
    expect(after.blocks.map((b) => b.text).filter((t) => t.startsWith("item"))).toContain("item two, improved");
    expect(new Set(after.blocks.map((b) => b.id)).size).toBe(after.blocks.length); // all ids unique
  });
});
