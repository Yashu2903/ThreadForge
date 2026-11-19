/**
 * The unsent/offline edit inside a block the AI replaced (docs/ai.md 19.6), on the REAL path: the real FastAPI app,
 * PostgreSQL, the collaboration server, real Hocuspocus providers, and the server's real apply code.
 *
 * The browser-side capture (frontend/src/collab/pending.ts) is attached to each client's Y.Doc exactly the way the
 * browser's CollabSession attaches it: it listens to REMOTE transactions, and the provider's own
 * `unsyncedChanges === 0` event marks "the server has acknowledged everything".
 */
import { type PMJson, blockHash, extractText } from "@threadforge/editor-schema";
import { YJS_FIELD } from "@threadforge/editor-schema";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";
import { type PendingPiece, PendingTracker } from "../../frontend/src/collab/pending";
import { all, createProposal, interchange, pool, scene } from "./fixtures";
import { type Client, connect, docJson, sleep, startCollab, stopAll, until } from "./harness";

afterEach(stopAll);

const texts = (client: Client) => ((docJson(client.ydoc).content ?? []) as PMJson[]).map((b) => extractText({ type: "doc", content: [b] }));

function track(client: Client) {
  const tracker = new PendingTracker(client.ydoc);
  const lost: PendingPiece[] = [];
  client.ydoc.on("afterTransaction", (tr: Y.Transaction) => {
    if (!tr.local) lost.push(...tracker.lostIn(tr));
  });
  client.provider.on("unsyncedChanges", ({ number }: { number: number }) => {
    if (number === 0) tracker.acknowledge();
  });
  return { tracker, lost };
}

/** wait until the server has acknowledged everything this client created */
const settled = (client: Client) => until(() => client.provider.unsyncedChanges === 0, "the server's acknowledgement", 10_000);

const typeAtEnd = (client: Client, block: number, text: string) => {
  const t = (client.ydoc.getXmlFragment(YJS_FIELD).get(block) as Y.XmlElement).get(0) as Y.XmlText;
  t.insert(t.length, text);
};

async function setup() {
  const s = await scene(interchange("Alpha", "Bravo", "Charlie"));
  const server = await startCollab({ aiApplyQuietMs: 0, aiApplyMaxWaitMs: 2_000, aiApplySweepMs: 0, aiApplyStuckAfterMs: 0 });
  const owner = connect(server, s.owner, s.docId);
  await owner.ready;
  const blocks = (docJson(owner.ydoc).content ?? []) as PMJson[];
  const ids = blocks.map((b) => b.attrs!.id as string);
  const hashes = await Promise.all(blocks.map((b) => blockHash(b)));
  return { s, server, owner, ids, hashes };
}
type Ctx = Awaited<ReturnType<typeof setup>>;

async function editorTab(ctx: Ctx) {
  const client = connect(ctx.server, ctx.s.editor, ctx.s.docId);
  await client.ready;
  await settled(client);
  return { client, ...track(client) };
}

/** a proposal that replaces block `i`'s text, applied through the server's real apply route */
async function aiReplaces(ctx: Ctx, i: number, newText: string) {
  const id = await createProposal({
    roomId: ctx.s.roomId,
    documentId: ctx.s.docId,
    user: ctx.s.owner,
    operations: [{ op: "replace_block_text", block_id: ctx.ids[i], new_inline: [{ text: newText, marks: [] }] }],
    baseHashes: { [ctx.ids[i]!]: ctx.hashes[i]! },
    scope: { whole_ids: [ctx.ids[i]!], ranges: [], anchors: ["start", ctx.ids[i]!] },
  });
  const reply = await apply(ctx, id);
  return { id, reply };
}
const apply = (ctx: Ctx, proposalId: string) => ctx.server.applier.apply({ proposal_id: proposalId, document_id: ctx.s.docId, user_id: ctx.s.owner.id });

/** the document as PostgreSQL holds it right now */
async function stored(ctx: Ctx): Promise<string[]> {
  const rows = await all<{ state: Buffer }>("SELECT state FROM document_yjs_state WHERE document_id = $1", [ctx.s.docId]);
  const copy = new Y.Doc();
  Y.applyUpdate(copy, new Uint8Array(rows[0]!.state));
  const json = (await import("@tiptap/y-tiptap")).yDocToProsemirrorJSON(copy, YJS_FIELD) as PMJson;
  return (json.content ?? []).map((b) => extractText({ type: "doc", content: [b] }));
}

describe("an offline edit inside a block the AI replaced (real server, PostgreSQL, real providers)", () => {
  it("the AI's text is accepted everywhere, the offline words are kept in the recovery copy, and nobody is corrupted", async () => {
    const ctx = await setup();
    const tab = await editorTab(ctx);
    // 1. the editor goes offline and types inside "Bravo"; nothing is sent
    tab.client.provider.disconnect();
    typeAtEnd(tab.client, 1, " [offline note]");
    expect(texts(tab.client)[1]).toBe("Bravo [offline note]");
    // 2. meanwhile the AI proposal is applied (the server cannot see that edit: the base hash still matches)
    const { id, reply } = await aiReplaces(ctx, 1, "Bravo (AI)");
    expect(reply.status, JSON.stringify(reply.body)).toBe(200);
    expect(reply.body).toMatchObject({ state: "applied", durable: true });
    // 3. the editor reconnects
    tab.client.provider.connect();
    await until(() => texts(tab.client)[1] === "Bravo (AI)", "the editor received the AI's text", 10_000);
    await settled(tab.client);
    // 4. everyone converges on the AI's text...
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]);
    expect(texts(tab.client)).toEqual(texts(ctx.owner));
    await until(async () => (await stored(ctx))[1] === "Bravo (AI)", "PostgreSQL holds the AI's text");
    expect(await stored(ctx)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]);
    // ...and the words are not in the shared document, but ARE in the recovery copy, with where they came from
    expect(tab.lost).toHaveLength(1);
    expect(tab.lost[0]).toMatchObject({ blockId: ctx.ids[1], inserted: " [offline note]", yourVersion: "Bravo [offline note]" });
    // the accepted proposal is intact and recorded exactly once
    const [row] = await all<{ state: string }>("SELECT state FROM ai_proposals WHERE id = $1", [id]);
    expect(row!.state).toBe("applied");
    const receipts = await all("SELECT 1 FROM ai_proposal_applications WHERE proposal_id = $1", [id]);
    expect(receipts).toHaveLength(1);
  });

  it("repeated reconnects and a repeated Apply neither duplicate the recovery copy nor change the document", async () => {
    const ctx = await setup();
    const tab = await editorTab(ctx);
    tab.client.provider.disconnect();
    typeAtEnd(tab.client, 1, " [once]");
    const { id } = await aiReplaces(ctx, 1, "Bravo (AI)");
    for (let i = 0; i < 3; i += 1) {
      tab.client.provider.connect();
      await until(() => texts(tab.client)[1] === "Bravo (AI)", "synced", 10_000);
      await settled(tab.client);
      tab.client.provider.disconnect();
    }
    tab.client.provider.connect();
    await settled(tab.client);
    const again = await apply(ctx, id); // Apply pressed a second time
    expect(again.status).toBe(200);
    expect(again.body).toMatchObject({ already: true });
    await sleep(300);
    expect(tab.lost.map((p) => p.inserted)).toEqual([" [once]"]);
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]);
    expect(texts(tab.client)).toEqual(texts(ctx.owner));
    expect(await all("SELECT 1 FROM ai_proposal_applications WHERE proposal_id = $1", [id])).toHaveLength(1);
  });

  it("an offline edit in a block the AI did not touch survives and reaches everyone", async () => {
    const ctx = await setup();
    const tab = await editorTab(ctx);
    tab.client.provider.disconnect();
    typeAtEnd(tab.client, 2, " [kept]");
    await aiReplaces(ctx, 1, "Bravo (AI)");
    tab.client.provider.connect();
    await until(() => texts(ctx.owner)[2] === "Charlie [kept]", "the offline edit reached the owner", 10_000);
    await settled(tab.client);
    expect(texts(tab.client)).toEqual(["Alpha", "Bravo (AI)", "Charlie [kept]"]);
    expect(tab.lost).toEqual([]);
  });

  it("several disconnected browsers each keep their own text", async () => {
    const ctx = await setup();
    const a = await editorTab(ctx);
    const b = await editorTab(ctx);
    a.client.provider.disconnect();
    b.client.provider.disconnect();
    typeAtEnd(a.client, 1, " [from A]");
    typeAtEnd(b.client, 1, " [from B]");
    await aiReplaces(ctx, 1, "Bravo (AI)");
    a.client.provider.connect();
    b.client.provider.connect();
    await Promise.all([settled(a.client), settled(b.client)]);
    await until(() => [a, b].every((t) => texts(t.client)[1] === "Bravo (AI)"), "everyone has the AI's text", 10_000);
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]);
    expect(a.lost.map((p) => p.inserted)).toEqual([" [from A]"]);
    expect(b.lost.map((p) => p.inserted)).toEqual([" [from B]"]);
  });

  it("is kept when the collaboration service restarted after the proposal was durably applied", async () => {
    const ctx = await setup();
    const tab = await editorTab(ctx);
    tab.client.provider.disconnect();
    typeAtEnd(tab.client, 1, " [across a restart]");
    const { reply } = await aiReplaces(ctx, 1, "Bravo (AI)");
    expect(reply.body).toMatchObject({ durable: true });
    tab.client.provider.destroy();
    ctx.owner.provider.destroy();
    await ctx.server.stop(); // the service goes away; the change is already in PostgreSQL
    await pool.query("UPDATE collab_lease SET holder_id = NULL, expires_at = now()");
    const second = await startCollab({ aiApplyQuietMs: 0, aiApplySweepMs: 0, aiApplyStuckAfterMs: 0 });
    // the same browser document (with its offline edit) reconnects to the NEW service
    const back = connect(second, ctx.s.editor, ctx.s.docId, { document: tab.client.ydoc });
    await back.ready;
    await until(() => texts(back)[1] === "Bravo (AI)", "the restarted service served the applied document", 10_000);
    expect(tab.lost.map((p) => p.inserted)).toEqual([" [across a restart]"]);
    expect(await stored(ctx)).toEqual(["Alpha", "Bravo (AI)", "Charlie"]);
  });

  it("a person who lost write access keeps their text locally, and the server does not accept it", async () => {
    const ctx = await setup();
    const tab = await editorTab(ctx);
    tab.client.provider.disconnect();
    typeAtEnd(tab.client, 1, " [typed before the downgrade]");
    await pool.query("UPDATE memberships SET role = 'viewer' WHERE room_id = $1 AND user_id = $2", [ctx.s.roomId, ctx.s.editor.id]);
    tab.client.provider.connect(); // reconnects read-only
    await until(() => tab.client.provider.isAuthenticated, "reconnected", 10_000).catch(() => undefined);
    await sleep(800);
    // writes stopped: the shared document and PostgreSQL never received it
    expect(texts(ctx.owner)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect(await stored(ctx)).toEqual(["Alpha", "Bravo", "Charlie"]);
    // the person's device still holds it, and it is available to be kept
    const kept = tab.tracker.alive();
    expect(kept).toHaveLength(1);
    expect(kept[0]).toMatchObject({ blockId: ctx.ids[1], inserted: " [typed before the downgrade]" });
  });
});
