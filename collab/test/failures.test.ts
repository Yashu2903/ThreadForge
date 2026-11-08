import { randomUUID } from "node:crypto";
import * as Y from "yjs";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError } from "../src/api";
import { CollabServer } from "../src/server";
import { all, createAsset, interchange, one, pool, scene } from "./fixtures";
import { FaultyApi, appendText, connect, docText, internalApi, sleep, startCollab, stopAll, testConfig, until } from "./harness";

afterEach(stopAll);

const stateRow = (docId: string) => one<{ update_counter: string; byte_size: number }>("SELECT update_counter, byte_size FROM document_yjs_state WHERE document_id = $1", [docId]);
const collabState = async (docId: string) => (await one<{ collab_state: string }>("SELECT collab_state FROM documents WHERE id = $1", [docId])).collab_state;
const snapshots = (docId: string) => all<{ kind: string; content: { content: unknown[] }; yjs_update_counter: string }>("SELECT kind, content, yjs_update_counter FROM document_snapshots WHERE document_id = $1 ORDER BY created_at", [docId]);

function addElement(ydoc: Y.Doc, name: string, attrs: Record<string, unknown>): Y.XmlElement {
  const element = new Y.XmlElement(name);
  for (const [k, v] of Object.entries(attrs)) element.setAttribute(k, v as string);
  ydoc.getXmlFragment("default").insert(ydoc.getXmlFragment("default").length, [element]);
  return element;
}

describe("storing", () => {
  it("retries a failed store and gets the data to PostgreSQL", async () => {
    const s = await scene(interchange("Retry"));
    const api = new FaultyApi();
    const server = await startCollab({ storeRetries: 3 }, { api });
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    api.failPuts = 2;
    appendText(a.ydoc, " survived");
    await until(async () => Number((await stateRow(s.docId)).update_counter) > 0, "store after retries");
    expect(api.failPuts).toBe(0);
  });

  it("keeps the document in memory through a longer outage and stores it when the API is back", async () => {
    const s = await scene(interchange("Outage"));
    const api = new FaultyApi();
    const server = await startCollab({ storeRetries: 1, storeRecoveryDelayMs: 150 }, { api });
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    api.failPuts = 5; // the first attempt (2 tries) fails, so does the next scheduled one
    appendText(a.ydoc, " later");
    await sleep(500);
    expect(Number((await stateRow(s.docId)).update_counter)).toBe(0);
    await until(async () => Number((await stateRow(s.docId)).update_counter) > 0, "recovery store", 10_000);
    const fresh = connect(server, s.editor, s.docId);
    await fresh.ready;
    expect(docText(fresh.ydoc)).toBe("Outage later");
  });

  it("a database outage while loading is reported as unavailable and does NOT mark the document", async () => {
    const s = await scene();
    const api = new FaultyApi();
    const server = await startCollab({}, { api });
    api.failGetState = 1;
    await expect(connect(server, s.owner, s.docId).ready).rejects.toThrow("denied:unavailable");
    expect(await collabState(s.docId)).toBe("legacy");
    const retry = connect(server, s.owner, s.docId);
    await retry.ready; // the next attempt works
  });
});

describe("the lease", () => {
  it("a second instance cannot take the lease while the first holds it", async () => {
    await startCollab();
    const second = new CollabServer(testConfig(), { onLeaseLost: () => undefined });
    await expect(second.lease.acquire(900, 150)).rejects.toMatchObject({ code: "lease_held" });
  });

  it("an instance that lost the lease cannot write, and reports it", async () => {
    const s = await scene(interchange("Fence"));
    let lost = 0;
    const server = await startCollab({ leaseRenewMs: 60_000 }, { onLeaseLost: () => void (lost += 1) });
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    // someone else takes over (as after a long pause of this process)
    await pool.query("UPDATE collab_lease SET expires_at = now() - interval '1 second'");
    await internalApi().acquireLease("another-instance-0001");
    appendText(a.ydoc, " STALE");
    await until(() => lost > 0, "lease loss detected by the failing write");
    await sleep(300);
    expect(Number((await stateRow(s.docId)).update_counter)).toBe(0);
    const health = await fetch(`http://127.0.0.1:${server.port}/health`);
    expect(health.status).toBe(503);
    expect(JSON.stringify(await snapshots(s.docId))).not.toContain("STALE");
  });

  it("renewal keeps the lease alive past its TTL", async () => {
    const server = await startCollab({ leaseRenewMs: 200 });
    const before = await one<{ expires_at: Date }>("SELECT expires_at FROM collab_lease");
    await sleep(900);
    const after = await one<{ expires_at: Date }>("SELECT expires_at FROM collab_lease");
    expect(after.expires_at.getTime()).toBeGreaterThan(before.expires_at.getTime());
    expect(server.lease.held).toBe(true);
  });
});

describe("snapshots and validation", () => {
  it("writes periodic and final snapshots with the update counter they correspond to", async () => {
    const s = await scene(interchange("Snap"));
    const server = await startCollab();
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    appendText(a.ydoc, " one");
    await until(async () => (await snapshots(s.docId)).some((x) => x.kind === "periodic"), "periodic snapshot");
    const periodic = (await snapshots(s.docId)).find((x) => x.kind === "periodic")!;
    expect(Number(periodic.yjs_update_counter)).toBeGreaterThan(0);
    expect(JSON.stringify(periodic.content)).toContain("Snap one");
    appendText(a.ydoc, " two");
    await until(async () => Number((await stateRow(s.docId)).update_counter) >= 2, "second store");
    a.provider.destroy(); // last disconnect
    await until(async () => JSON.stringify(await snapshots(s.docId)).includes("Snap one two"), "final snapshot");
    expect(server.loadedDocuments).toBe(0);
  });

  it("an invalid structure marks needs_repair, keeps the raw Yjs, keeps the last valid snapshot and refuses new connections", async () => {
    const s = await scene(interchange("Valid text"));
    const server = await startCollab();
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    appendText(a.ydoc, "!");
    await until(async () => (await snapshots(s.docId)).some((x) => x.kind === "periodic"), "a valid snapshot first");
    const before = (await snapshots(s.docId)).length;

    addElement(a.ydoc, "heading", { level: 9, id: randomUUID() }); // heading level 9 is outside the schema
    await until(async () => (await collabState(s.docId)) === "needs_repair", "needs_repair");

    // the raw state still includes the bad node: what people typed is never discarded
    const raw = await internalApi().getState(s.docId);
    const probe = new Y.Doc();
    Y.applyUpdate(probe, raw.state);
    expect(probe.getXmlFragment("default").length).toBe(2);
    // and no invalid snapshot was written
    const after = await snapshots(s.docId);
    expect(after.length).toBe(before);
    expect(JSON.stringify(after)).not.toContain('"level":9');
    // new connections are refused until an operator repairs it
    await expect(connect(server, s.editor, s.docId).ready).rejects.toThrow("denied:needs_repair");
  });

  it("a live document that becomes valid again leaves needs_repair by itself", async () => {
    const s = await scene(interchange("Heal"));
    const server = await startCollab();
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    const bad = addElement(a.ydoc, "heading", { level: 9, id: randomUUID() });
    await until(async () => (await collabState(s.docId)) === "needs_repair", "needs_repair");
    a.ydoc.getXmlFragment("default").delete(a.ydoc.getXmlFragment("default").toArray().indexOf(bad), 1);
    await until(async () => (await collabState(s.docId)) === "ready", "ready again");
  });

  it("tolerates one tick of duplicate block ids (concurrent edits) before declaring the document broken", async () => {
    const s = await scene(interchange("Dup"));
    const server = await startCollab({ snapshotIntervalMs: 400 });
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    const id = randomUUID();
    const p1 = addElement(a.ydoc, "paragraph", { id });
    addElement(a.ydoc, "paragraph", { id });
    await sleep(700); // roughly one snapshot tick
    expect(await collabState(s.docId)).toBe("ready");
    a.ydoc.getXmlFragment("default").delete(a.ydoc.getXmlFragment("default").toArray().indexOf(p1), 1); // the editor's UniqueID repairs it
    await sleep(900);
    expect(await collabState(s.docId)).toBe("ready");
  });

  it("derives asset references from snapshots and treats a missing asset as invalid", async () => {
    const s = await scene(interchange("Pic"));
    const server = await startCollab();
    const asset = await createAsset(s.roomId, s.owner);
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    const paragraph = a.ydoc.getXmlFragment("default").get(0) as Y.XmlElement;
    const image = new Y.XmlElement("image");
    image.setAttribute("assetId", asset);
    image.setAttribute("alt", "");
    image.setAttribute("width", 10 as never);
    image.setAttribute("height", 10 as never);
    paragraph.insert(paragraph.length, [image]);
    await until(async () => (await all("SELECT 1 FROM document_asset_refs WHERE document_id = $1", [s.docId])).length === 1, "reference created");
    expect((await one<{ state: string }>("SELECT state FROM room_assets WHERE id = $1", [asset])).state).toBe("attached");

    const missing = new Y.XmlElement("image");
    missing.setAttribute("assetId", randomUUID());
    missing.setAttribute("alt", "");
    missing.setAttribute("width", 10 as never);
    missing.setAttribute("height", 10 as never);
    paragraph.insert(paragraph.length, [missing]);
    await until(async () => (await collabState(s.docId)) === "needs_repair", "missing asset marks needs_repair");
  });
});

describe("seeding failures and restore", () => {
  it("a seed that cannot be adapted marks needs_repair and leaves documents.content untouched", async () => {
    const broken = { schema_version: 1, blocks: [{ type: "paragraph", id: randomUUID(), surprise: true }] };
    const s = await scene(broken);
    const server = await startCollab();
    await expect(connect(server, s.owner, s.docId).ready).rejects.toThrow("denied:needs_repair");
    expect(await collabState(s.docId)).toBe("needs_repair");
    expect((await one<{ content: unknown }>("SELECT content FROM documents WHERE id = $1", [s.docId])).content).toEqual(broken);
    expect(await all("SELECT 1 FROM document_yjs_state WHERE document_id = $1", [s.docId])).toHaveLength(0);
  });

  it("corrupt stored Yjs state is refused safely and marked", async () => {
    const s = await scene();
    const garbage = Buffer.from([255, 255, 255, 255, 1, 2, 3]);
    await pool.query("INSERT INTO document_yjs_state (document_id, room_id, state, update_counter, byte_size, updated_at) VALUES ($1, $2, $3, 0, $4, now())", [s.docId, s.roomId, garbage, garbage.length]);
    await pool.query("UPDATE documents SET collab_state = 'ready' WHERE id = $1", [s.docId]);
    const server = await startCollab();
    await expect(connect(server, s.owner, s.docId).ready).rejects.toThrow("denied:needs_repair");
    expect(await collabState(s.docId)).toBe("needs_repair");
  });

  it("a restored snapshot (operator repair) seeds the document again", async () => {
    const s = await scene(interchange("Old"));
    const restored = { type: "doc", content: [{ type: "paragraph", attrs: { id: randomUUID() }, content: [{ type: "text", text: "Restored text" }] }] };
    await pool.query(
      "INSERT INTO document_snapshots (document_id, room_id, kind, yjs_update_counter, content, content_hash, text_length, created_at) VALUES ($1, $2, 'restore', 0, $3, $4, 13, now())",
      [s.docId, s.roomId, JSON.stringify(restored), Buffer.alloc(32, 1)],
    );
    const server = await startCollab();
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    expect(docText(a.ydoc)).toBe("Restored text");
  });
});

describe("limits", () => {
  it("closes a connection that sends a message larger than the limit, before parsing it", async () => {
    const s = await scene(interchange("Small"));
    const server = await startCollab({ maxMessageBytes: 100 * 1024 });
    const a = connect(server, s.owner, s.docId);
    const b = connect(server, s.editor, s.docId);
    await Promise.all([a.ready, b.ready]);
    appendText(a.ydoc, "x".repeat(300 * 1024));
    await until(() => a.closes.some((c) => c.code === 1009), "close 1009 (message too big)");
    expect(docText(b.ydoc)).toBe("Small");
  });

  it("at the maximum state size the document becomes read-only for everyone", async () => {
    const s = await scene(interchange("Cap"));
    const server = await startCollab({ maxStateBytes: 20_000 });
    const a = connect(server, s.owner, s.docId);
    const b = connect(server, s.editor, s.docId);
    await Promise.all([a.ready, b.ready]);
    const stored = Number((await stateRow(s.docId)).byte_size);
    appendText(a.ydoc, "y".repeat(30_000));
    await until(() => [...server.hocuspocus.documents.get(`doc:${s.docId}`)!.getConnections()].every((c) => c.readOnly), "frozen");
    appendText(b.ydoc, " late edit");
    await sleep(500);
    expect(Number((await stateRow(s.docId)).byte_size)).toBeLessThanOrEqual(Math.max(stored, 20_000));
    // a new connection is read-only too
    const c = connect(server, s.owner, s.docId);
    await c.ready;
    expect(c.provider.authorizedScope).toBe("readonly");
  });
});

it("internal API errors carry a code, never a body", async () => {
  const api = internalApi();
  await expect(api.getSeed("00000000-0000-4000-8000-000000000000")).rejects.toMatchObject({ status: 404, code: "document_not_found" });
  expect(new ApiError(0, "unreachable").transient).toBe(true);
});
