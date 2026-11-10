/**
 * Applying a reviewed AI proposal to the live shared document: the authoritative path (collab/src/apply.ts).
 * Real PostgreSQL, the real FastAPI app, the real collaboration server and real Hocuspocus clients.
 */
import { type PMJson, YJS_FIELD, blockHash, extractText } from "@threadforge/editor-schema";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";
import { ApiError, type ApplyReceipt, InternalApi } from "../src/api";
import { MARKER_MAP } from "../src/yapply";
import { API_URL, INTERNAL_SECRET } from "./env";
import { all, createProposal, createUser, addMember, one, pool, scene, interchange } from "./fixtures";
import { type Client, connect, docJson, docText, startCollab, stopAll, until } from "./harness";

afterEach(stopAll);

const NEW = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const para = (text: string) => ({ type: "paragraph", inline: [{ text, marks: [] }] });

/** An API client that can be told to fail, to rehearse a crash at an exact point. */
class FlakyApi extends InternalApi {
  failStoreWithReceipt = false;
  failFinish = false;
  override async putState(documentId: string, token: number, counter: number, state: Uint8Array, insertIfAbsent = false, receipt?: ApplyReceipt): Promise<void> {
    if (receipt && this.failStoreWithReceipt) throw new ApiError(0, "unreachable");
    return super.putState(documentId, token, counter, state, insertIfAbsent, receipt);
  }
  override async finishApply(...args: Parameters<InternalApi["finishApply"]>) {
    if (this.failFinish) throw new ApiError(0, "unreachable");
    return super.finishApply(...args);
  }
}

async function setup(texts = ["Alpha", "Bravo", "Charlie"], overrides: Record<string, unknown> = {}, flaky?: FlakyApi) {
  const s = await scene(interchange(...texts));
  const server = await startCollab({ aiApplyQuietMs: 0, aiApplyMaxWaitMs: 2_000, aiApplySweepMs: 0, aiApplyStuckAfterMs: 0, ...overrides }, flaky ? { api: flaky } : {});
  const owner = connect(server, s.owner, s.docId);
  await owner.ready;
  const blocks = (docJson(owner.ydoc).content ?? []) as PMJson[];
  const ids = blocks.map((b) => b.attrs!.id as string);
  const hashes = await Promise.all(blocks.map((b) => blockHash(b)));
  return { s, server, owner, ids, hashes };
}
type Ctx = Awaited<ReturnType<typeof setup>>;

function propose(ctx: Ctx, operations: unknown[], targets: number[], extra: { anchors?: string[]; state?: string; expiresInSeconds?: number } = {}) {
  return createProposal({
    roomId: ctx.s.roomId,
    documentId: ctx.s.docId,
    user: ctx.s.owner,
    operations,
    baseHashes: Object.fromEntries(targets.map((i) => [ctx.ids[i]!, ctx.hashes[i]!])),
    scope: { whole_ids: targets.map((i) => ctx.ids[i]!), ranges: [], anchors: extra.anchors ?? ["start", ...targets.map((i) => ctx.ids[i]!)] },
    state: extra.state,
    expiresInSeconds: extra.expiresInSeconds,
  });
}

const apply = (ctx: Ctx, proposalId: string, userId = ctx.s.owner.id) => ctx.server.applier.apply({ proposal_id: proposalId, document_id: ctx.s.docId, user_id: userId });
const stateOf = async (id: string) => (await one<{ state: string; resolution_code: string | null }>("SELECT state, resolution_code FROM ai_proposals WHERE id = $1", [id]));
const texts = (client: Client) => ((docJson(client.ydoc).content ?? []) as PMJson[]).map((b) => extractText({ type: "doc", content: [b] }));

describe("applying a proposal", () => {
  it("applies a replacement and an insertion; two real editors converge; the receipt and marker exist", async () => {
    const ctx = await setup();
    const editor = connect(ctx.server, ctx.s.editor, ctx.s.docId);
    await editor.ready;
    const id = await propose(ctx, [
      { op: "replace_block_text", block_id: ctx.ids[1], new_inline: [{ text: "Bravo, rewritten", marks: [] }] },
      { op: "insert_blocks_after", anchor_block_id: ctx.ids[1], blocks: [para("Inserted after Bravo")] },
    ], [1]);
    const reply = await apply(ctx, id);
    expect(reply.status, JSON.stringify(reply.body)).toBe(200);
    expect(reply.body).toMatchObject({ state: "applied", already: false, synced: true, durable: true });
    // both editors converge on the same document
    await until(() => texts(editor).join("|") === "Alpha|Bravo, rewritten|Inserted after Bravo|Charlie");
    expect(texts(ctx.owner)).toEqual(texts(editor));
    // the block element and its id survived the replacement
    expect((docJson(ctx.owner.ydoc).content as PMJson[])[1]!.attrs!.id).toBe(ctx.ids[1]);
    // PostgreSQL: applied, a receipt in the same transaction as the state, resolved_by recorded
    expect(await stateOf(id)).toEqual({ state: "applied", resolution_code: "applied" });
    const [receipt] = await all<{ resulting_counter: string; applied_by: string; len: number }>(
      "SELECT resulting_counter::text, applied_by, octet_length(receipt_hash) AS len FROM ai_proposal_applications WHERE proposal_id = $1",
      [id],
    );
    expect(receipt!.len).toBe(32);
    expect(receipt!.applied_by).toBe(ctx.s.owner.id);
    const stored = await all<{ update_counter: string }>("SELECT update_counter::text FROM document_yjs_state WHERE document_id = $1", [ctx.s.docId]);
    expect(Number(receipt!.resulting_counter)).toBe(Number(stored[0]!.update_counter));
    // the idempotency marker is part of the shared document
    const doc = ctx.server.hocuspocus.documents.get(`doc:${ctx.s.docId}`)!;
    expect(doc.getMap(MARKER_MAP).has(id)).toBe(true);
    // and the stored state holds the change: load it fresh
    const fresh = await all<{ state: Buffer }>("SELECT state FROM document_yjs_state WHERE document_id = $1", [ctx.s.docId]);
    const copy = new Y.Doc();
    Y.applyUpdate(copy, new Uint8Array(fresh[0]!.state));
    expect(copy.getMap(MARKER_MAP).has(id)).toBe(true);
    expect(copy.getXmlFragment(YJS_FIELD).length).toBe(4);
  });

  it("deletes blocks and inserts at the start", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [
      { op: "delete_blocks", block_ids: [ctx.ids[2]] },
      { op: "insert_blocks_after", anchor_block_id: "start", blocks: [para("Top")] },
    ], [2]);
    expect((await apply(ctx, id)).status).toBe(200);
    expect(texts(ctx.owner)).toEqual(["Top", "Alpha", "Bravo"]);
  });

  it("a target that changed gives conflicted, changes nothing, and the proposal is conflicted", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [{ op: "replace_block_text", block_id: ctx.ids[1], new_inline: [{ text: "Nope", marks: [] }] }], [1]);
    // a person edits the target block after the proposal was made
    const paragraph = ctx.owner.ydoc.getXmlFragment(YJS_FIELD).get(1) as Y.XmlElement;
    const text = paragraph.get(0) as Y.XmlText;
    text.insert(text.length, " edited");
    await until(async () => (await ctx.server.msSinceHumanUpdate(`doc:${ctx.s.docId}`)) < 1000);
    const before = texts(ctx.owner).join("|");
    const reply = await apply(ctx, id);
    expect(reply.status).toBe(409);
    expect(reply.body).toMatchObject({ error: "conflicted", code: "block_changed" });
    expect(texts(ctx.owner).join("|")).toBe(before);
    expect(await stateOf(id)).toEqual({ state: "conflicted", resolution_code: "block_changed" });
    expect(await all("SELECT 1 FROM ai_proposal_applications WHERE proposal_id = $1", [id])).toHaveLength(0);
    // a conflicted proposal cannot be applied again
    expect((await apply(ctx, id)).status).toBe(409);
  });

  it("a target block that was deleted is a conflict too", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [{ op: "replace_block_text", block_id: ctx.ids[1], new_inline: [{ text: "x", marks: [] }] }], [1]);
    ctx.owner.ydoc.getXmlFragment(YJS_FIELD).delete(1, 1);
    const reply = await apply(ctx, id);
    expect(reply.body).toMatchObject({ error: "conflicted", code: "block_missing" });
  });

  it("is all or nothing: one operation that cannot be applied leaves the whole document untouched", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [
      { op: "replace_block_text", block_id: ctx.ids[1], new_inline: [{ text: "Changed", marks: [] }] },
      { op: "delete_blocks", block_ids: [NEW(77)] }, // a block that does not exist
    ], [1]);
    const before = texts(ctx.owner).join("|");
    const reply = await apply(ctx, id);
    expect(reply.status).toBe(409);
    expect(texts(ctx.owner).join("|")).toBe(before);
    expect(docJson(ctx.owner.ydoc).content).toHaveLength(3);
    expect((await stateOf(id)).state).toBe("conflicted");
  });

  it("a candidate that fails the document rules is refused as a whole", async () => {
    const ctx = await setup();
    const hostile = { type: "diagram", mermaid: 'flowchart TD\n  click A href "javascript:x"' };
    const id = await propose(ctx, [
      { op: "replace_block_text", block_id: ctx.ids[1], new_inline: [{ text: "Fine", marks: [] }] },
      { op: "insert_blocks_after", anchor_block_id: ctx.ids[1], blocks: [hostile] },
    ], [1]);
    const reply = await apply(ctx, id);
    expect(reply.body).toMatchObject({ error: "conflicted" });
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie"]);
  });

  it("the quiet guard answers document_busy while people are typing, keeps the proposal pending, and succeeds later", async () => {
    const ctx = await setup(["Alpha", "Bravo", "Charlie"], { aiApplyQuietMs: 700, aiApplyMaxWaitMs: 200 });
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[0], blocks: [para("New")] }], [0]);
    // Alpha is not typed in; ANOTHER block is (the guard is document-level, not block-level)
    const text = (ctx.owner.ydoc.getXmlFragment(YJS_FIELD).get(2) as Y.XmlElement).get(0) as Y.XmlText;
    text.insert(text.length, "!");
    await until(() => ctx.server.msSinceHumanUpdate(`doc:${ctx.s.docId}`) < 300);
    const busy = await apply(ctx, id);
    expect(busy.status).toBe(409);
    expect(busy.body).toMatchObject({ error: "document_busy", retryable: true });
    expect((await stateOf(id)).state).toBe("pending");
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie!"]);
    await new Promise((resolve) => setTimeout(resolve, 800)); // quiet again
    ctx.server.config.aiApplyMaxWaitMs = 2_000;
    const ok = await apply(ctx, id);
    expect(ok.status).toBe(200);
    expect(texts(ctx.owner)).toEqual(["Alpha", "New", "Bravo", "Charlie!"]);
  });

  it("a duplicate apply and a concurrent apply produce ONE change (compare-and-set and marker)", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("Once")] }], [2]);
    const [a, b] = await Promise.all([apply(ctx, id), apply(ctx, id)]);
    expect([a.status, b.status]).toEqual([200, 200]);
    const again = await apply(ctx, id);
    expect(again.status).toBe(200);
    expect(again.body).toMatchObject({ state: "applied", already: true });
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie", "Once"]);
    expect(await all("SELECT 1 FROM ai_proposal_applications WHERE proposal_id = $1", [id])).toHaveLength(1);
  });

  it("the database compare-and-set lets only one of two competing begins win", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("X")] }], [2]);
    const api = new InternalApi(API_URL, INTERNAL_SECRET, 10_000);
    const token = ctx.server.lease.token;
    const other = await createUser("Other editor");
    await addMember(ctx.s.roomId, other, "editor");
    const results = await Promise.allSettled([
      api.beginApply(id, token, { user_id: ctx.s.owner.id, document_id: ctx.s.docId }),
      api.beginApply(id, token, { user_id: other.id, document_id: ctx.s.docId }),
    ]);
    expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(1);
    const lost = results.find((r) => r.status === "rejected") as PromiseRejectedResult;
    expect((lost.reason as ApiError).code).toBe("proposal_applying");
    // leave nothing behind: a proposal stuck in `applying` would be picked up by the next test's sweeper
    await api.releaseApply(id, token, "test_cleanup");
    expect((await stateOf(id)).state).toBe("pending");
  });

  it("refuses a stale lease: nothing changes", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("X")] }], [2]);
    await pool.query("UPDATE collab_lease SET fencing_token = fencing_token + 5");
    const reply = await apply(ctx, id);
    expect(reply.status).toBe(409);
    expect(reply.body).toMatchObject({ error: "stale_lease" });
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect((await stateOf(id)).state).toBe("pending");
  });

  it("checks write access at apply time: a viewer, a removed member and an outsider cannot apply", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("X")] }], [2]);
    expect((await apply(ctx, id, ctx.s.viewer.id)).status).toBe(403);
    const outsider = await createUser("Outsider");
    expect((await apply(ctx, id, outsider.id)).status).toBe(403);
    await pool.query("UPDATE memberships SET state = 'removed', removed_at = now() WHERE user_id = $1", [ctx.s.editor.id]);
    expect((await apply(ctx, id, ctx.s.editor.id)).status).toBe(403);
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect((await stateOf(id)).state).toBe("pending");
    // another editor with current write access may apply someone else's proposal
    const second = await createUser("Second editor");
    await addMember(ctx.s.roomId, second, "editor");
    expect((await apply(ctx, id, second.id)).status).toBe(200);
  });

  it("refuses a proposal for another document, an expired one, and one in a final state", async () => {
    const ctx = await setup();
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("X")] }], [2]);
    const wrong = await ctx.server.applier.apply({ proposal_id: id, document_id: NEW(5), user_id: ctx.s.owner.id });
    expect(wrong.status).toBe(404);
    const expired = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("X")] }], [2], { expiresInSeconds: 2 });
    await pool.query("UPDATE ai_proposals SET expires_at = now() - interval '1 second', created_at = now() - interval '1 day' WHERE id = $1", [expired]);
    expect((await apply(ctx, expired)).body).toMatchObject({ error: "proposal_expired" });
    expect((await stateOf(expired)).state).toBe("expired");
    for (const state of ["rejected", "superseded", "conflicted"]) {
      const done = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("X")] }], [2], { state });
      expect((await apply(ctx, done)).body).toMatchObject({ error: `proposal_${state}` });
    }
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie"]);
  });

  it("keeps targeting the original document when the room's active document changes", async () => {
    const ctx = await setup();
    const other = await pool.query(
      "INSERT INTO documents (room_id, created_by, title, template_key, content, content_hash, created_at, updated_at, content_updated_at)" +
        " VALUES ($1, $2, 'Other', 'blank', $3, decode(repeat('00', 32), 'hex'), now(), now(), now()) RETURNING id",
      [ctx.s.roomId, ctx.s.owner.id, JSON.stringify(interchange("Other doc"))],
    );
    await pool.query("UPDATE rooms SET active_document_id = $1, active_document_version = active_document_version + 1 WHERE id = $2", [other.rows[0].id, ctx.s.roomId]);
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("Mine")] }], [2]);
    expect((await apply(ctx, id)).status).toBe(200);
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie", "Mine"]);
    const stateRow = await all("SELECT 1 FROM document_yjs_state WHERE document_id = $1", [other.rows[0].id]);
    expect(stateRow).toHaveLength(0); // the other document was never even loaded
  });
});

describe("crashes, and the recovery sweeper", () => {
  it("a store that fails after the change was broadcast: the editors have it, the proposal stays applying, the sweeper finishes it", async () => {
    const flaky = new FlakyApi(API_URL, INTERNAL_SECRET, 10_000);
    const ctx = await setup(["Alpha", "Bravo", "Charlie"], {}, flaky);
    const editor = connect(ctx.server, ctx.s.editor, ctx.s.docId);
    await editor.ready;
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("Broadcast")] }], [2]);
    flaky.failStoreWithReceipt = true;
    const reply = await apply(ctx, id);
    expect(reply.status).toBe(503);
    // connected editors hold the change (it was broadcast) ...
    await until(() => texts(editor).includes("Broadcast"));
    // ... but it is not durable and the proposal is not applied
    expect((await stateOf(id)).state).toBe("applying");
    expect(await all("SELECT 1 FROM ai_proposal_applications WHERE proposal_id = $1", [id])).toHaveLength(0);
    flaky.failStoreWithReceipt = false;
    expect(await ctx.server.applier.sweep(0)).toBe(1);
    expect((await stateOf(id)).state).toBe("applied");
    expect(await all("SELECT 1 FROM ai_proposal_applications WHERE proposal_id = $1", [id])).toHaveLength(1);
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie", "Broadcast"]);
  });

  it("a crash after the store committed but before the proposal was marked applied: the sweeper marks it, without a second change", async () => {
    const flaky = new FlakyApi(API_URL, INTERNAL_SECRET, 10_000);
    const ctx = await setup(["Alpha", "Bravo", "Charlie"], {}, flaky);
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("Durable")] }], [2]);
    flaky.failFinish = true;
    expect((await apply(ctx, id)).status).toBe(503);
    expect((await stateOf(id)).state).toBe("applying");
    expect(await all("SELECT 1 FROM ai_proposal_applications WHERE proposal_id = $1", [id])).toHaveLength(1); // the receipt IS durable
    flaky.failFinish = false;
    expect(await ctx.server.applier.sweep(0)).toBe(1);
    expect((await stateOf(id)).state).toBe("applied");
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie", "Durable"]);
  });

  it("a restart after a crash: the stored marker finishes the proposal; no marker returns it to pending", async () => {
    const flaky = new FlakyApi(API_URL, INTERNAL_SECRET, 10_000);
    const ctx = await setup(["Alpha", "Bravo", "Charlie"], {}, flaky);
    const applied = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("Stored")] }], [2]);
    flaky.failFinish = true;
    await apply(ctx, applied);
    // a proposal that was taken (applying) but never changed anything
    const untouched = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[0], blocks: [para("Never")] }], [0], { state: "applying" });
    await stopAll();
    // a new service instance: its sweeper runs at start
    const restarted = await startCollab({ aiApplyQuietMs: 0, aiApplySweepMs: 60_000, aiApplyStuckAfterMs: 0 });
    await until(async () => (await stateOf(applied)).state === "applied" && (await stateOf(untouched)).state === "pending", "the sweeper resolved both", 8_000);
    const client = connect(restarted, ctx.s.owner, ctx.s.docId);
    await client.ready;
    expect(texts(client)).toEqual(["Alpha", "Bravo", "Charlie", "Stored"]);
  });
});

describe("what people see and can undo", () => {
  it("a person's undo does not undo an AI change, and the AI change carries its own origin", async () => {
    const ctx = await setup(["Alpha", "Bravo", "Charlie"]);
    const doc = ctx.server.hocuspocus.documents.get(`doc:${ctx.s.docId}`)!;
    const origins: unknown[] = [];
    doc.on("update", (_update: Uint8Array, origin: unknown) => origins.push(origin));
    // the editor's undo manager tracks only the person's own (local, editor-made) transactions
    const LOCAL = { person: true };
    const undo = new Y.UndoManager(ctx.owner.ydoc.getXmlFragment(YJS_FIELD), { trackedOrigins: new Set([LOCAL]) });
    ctx.owner.ydoc.transact(() => {
      const text = (ctx.owner.ydoc.getXmlFragment(YJS_FIELD).get(0) as Y.XmlElement).get(0) as Y.XmlText;
      text.insert(text.length, " (typed)");
    }, LOCAL);
    await new Promise((resolve) => setTimeout(resolve, 150));
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("From the AI")] }], [2]);
    expect((await apply(ctx, id)).status).toBe(200);
    await until(() => texts(ctx.owner).includes("From the AI"));
    undo.undo(); // Ctrl+Z
    expect(texts(ctx.owner)).toContain("From the AI"); // the AI change is still there
    expect(texts(ctx.owner)[0]).toBe("Alpha"); // the person's own typing was undone
    const aiOrigin = origins.find((o) => (o as { context?: { source?: string } })?.context?.source === "ai-apply");
    expect(aiOrigin).toBeTruthy();
    expect(undo.trackedOrigins.has(aiOrigin)).toBe(false);
  });

  it("documents what an offline client's UNSENT edit inside a replaced block does: it is dropped when it merges (a known limit)", async () => {
    const ctx = await setup(["Alpha", "Bravo", "Charlie"]);
    const offline = connect(ctx.server, ctx.s.editor, ctx.s.docId);
    await offline.ready;
    await until(() => texts(offline).length === 3);
    offline.provider.disconnect(); // the editor goes offline
    // while offline they type at the end of "Bravo" (their copy only, never sent)
    const text = (offline.ydoc.getXmlFragment(YJS_FIELD).get(1) as Y.XmlElement).get(0) as Y.XmlText;
    text.insert(text.length, " [offline note]");
    const id = await propose(ctx, [{ op: "replace_block_text", block_id: ctx.ids[1], new_inline: [{ text: "Bravo (AI)", marks: [] }] }], [1]);
    expect((await apply(ctx, id)).status).toBe(200); // the server cannot see the unsent edit: the hash check passes
    offline.provider.connect(); // they come back
    await until(() => texts(offline)[1] === "Bravo (AI)", "the offline client received the AI's text", 8_000);
    await new Promise((resolve) => setTimeout(resolve, 500)); // let anything pending settle
    // Replacing text deletes the old text node; Yjs discards content that a client inserted INSIDE a deleted node when
    // it merges. So the unsent words are gone on every side, and everyone converges on the AI's text. This is the
    // documented limit (docs/ai.md 19.6): a base hash protects the targeted blocks against edits the server has SEEN,
    // not against edits that were never sent. If this test starts failing because the text SURVIVES, the limit
    // changed: update the documentation, not only the test.
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]);
    expect(texts(offline)).toEqual(texts(ctx.owner));
  });
});

describe("the private route", () => {
  it("requires the shared secret, validates its input and answers JSON", async () => {
    const ctx = await setup();
    const url = `http://127.0.0.1:${ctx.server.port}/internal/ai/apply-proposal`;
    const call = (headers: Record<string, string>, body: unknown) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
    expect((await call({}, {})).status).toBe(401);
    expect((await call({ "X-Internal-Secret": "wrong-secret-wrong-secret" }, {})).status).toBe(401);
    expect((await call({ "X-Internal-Secret": INTERNAL_SECRET }, { proposal_id: "x" })).status).toBe(422);
    const id = await propose(ctx, [{ op: "insert_blocks_after", anchor_block_id: ctx.ids[2], blocks: [para("Via HTTP")] }], [2]);
    const ok = await call({ "X-Internal-Secret": INTERNAL_SECRET }, { proposal_id: id, document_id: ctx.s.docId, user_id: ctx.s.owner.id });
    expect(ok.status).toBe(200);
    expect(await ok.json()).toMatchObject({ state: "applied", synced: true, durable: true });
  });
});

void docText;
