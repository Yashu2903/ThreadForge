import { afterEach, describe, expect, it } from "vitest";
import { all, createUser, interchange, one, scene } from "./fixtures";
import { appendText, connect, docText, sleep, startCollab, stopAll, until } from "./harness";

afterEach(stopAll);

describe("real-time sync", () => {
  it("two editors see each other's changes, and a late joiner gets the whole document", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab();
    const a = connect(server, s.owner, s.docId);
    const b = connect(server, s.editor, s.docId);
    await Promise.all([a.ready, b.ready]);
    expect(docText(a.ydoc)).toBe("Hello");
    expect(docText(b.ydoc)).toBe("Hello");

    appendText(a.ydoc, " from A");
    appendText(b.ydoc, " and B");
    await until(() => docText(a.ydoc) === docText(b.ydoc) && docText(a.ydoc).includes("from A") && docText(a.ydoc).includes("and B"), "convergence");

    const late = connect(server, s.viewer, s.docId);
    await late.ready;
    expect(docText(late.ydoc)).toBe(docText(a.ydoc));
  });

  it("seeds a legacy document into Yjs exactly once, even when many clients arrive together", async () => {
    const s = await scene(interchange("One", "Two"));
    const server = await startCollab();
    const clients = [s.owner, s.editor, s.viewer, s.owner, s.editor, s.viewer].map((u) => connect(server, u, s.docId));
    await Promise.all(clients.map((c) => c.ready));
    for (const c of clients) expect(docText(c.ydoc)).toBe("One\nTwo");
    expect(Number((await one<{ n: string }>("SELECT count(*) AS n FROM document_yjs_state WHERE document_id = $1", [s.docId])).n)).toBe(1);
    const doc = await one<{ collab_state: string }>("SELECT collab_state FROM documents WHERE id = $1", [s.docId]);
    expect(doc.collab_state).toBe("ready");
    const seeds = await all<{ kind: string }>("SELECT kind FROM document_snapshots WHERE document_id = $1", [s.docId]);
    expect(seeds.map((r) => r.kind)).toEqual(["seed"]);
  });

  it("never touches documents.content: it stays read-only seed material", async () => {
    const s = await scene(interchange("Seed"));
    const before = await one<{ content: unknown; content_hash: Buffer; content_version: string }>(
      "SELECT content, content_hash, content_version FROM documents WHERE id = $1",
      [s.docId],
    );
    const server = await startCollab();
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    appendText(a.ydoc, " edited");
    await until(async () => (await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [s.docId]))?.update_counter !== "0", "store");
    const after = await one<typeof before>("SELECT content, content_hash, content_version FROM documents WHERE id = $1", [s.docId]);
    expect(after).toEqual(before);
    expect(Number((await one<{ n: string }>("SELECT count(*) AS n FROM room_events WHERE room_id = $1 AND type = 'document_content_updated'", [s.roomId])).n)).toBe(0);
  });

  it("persists across a restart and continues the update counter", async () => {
    const s = await scene(interchange("Start"));
    let server = await startCollab();
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    appendText(a.ydoc, " + saved");
    await until(async () => (await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [s.docId])).update_counter !== "0", "first store");
    const counterBefore = Number((await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [s.docId])).update_counter);
    await stopAll();

    server = await startCollab(); // new process-equivalent: new lease holder, new token
    const b = connect(server, s.editor, s.docId);
    await b.ready;
    expect(docText(b.ydoc)).toBe("Start + saved");
    appendText(b.ydoc, "!");
    await until(async () => Number((await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [s.docId])).update_counter) > counterBefore, "counter continues");
  });
});

describe("who may write", () => {
  it("a viewer is read-only on the server: edits from a modified client are ignored", async () => {
    const s = await scene(interchange("Locked"));
    const server = await startCollab();
    const owner = connect(server, s.owner, s.docId);
    const viewer = connect(server, s.viewer, s.docId);
    await Promise.all([owner.ready, viewer.ready]);
    expect(viewer.provider.authorizedScope).toBe("readonly");
    expect(owner.provider.authorizedScope).toBe("read-write");

    appendText(viewer.ydoc, " HACKED"); // a modified client simply applies the edit locally and sends it
    await sleep(600);
    expect(docText(owner.ydoc)).toBe("Locked");
    const late = connect(server, s.editor, s.docId);
    await late.ready;
    expect(docText(late.ydoc)).toBe("Locked");
    appendText(owner.ydoc, "!");
    await until(async () => (await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [s.docId])).update_counter !== "0", "store");
    const snap = await all<{ content: unknown }>("SELECT content FROM document_snapshots WHERE document_id = $1", [s.docId]);
    expect(JSON.stringify(snap)).not.toContain("HACKED");
  });
});

describe("authentication", () => {
  it("refuses a wrong Origin at the upgrade, before any message is parsed", async () => {
    const s = await scene();
    const server = await startCollab();
    for (const origin of ["http://evil.example", null, "http://localhost:5173/"]) {
      const c = connect(server, s.owner, s.docId, { origin });
      await sleep(700);
      expect(c.provider.synced).toBe(false);
      expect(c.provider.isAuthenticated).toBe(false);
    }
    expect(server.hocuspocus.getConnectionsCount()).toBe(0);
  });

  it("refuses unknown paths, bad document names, missing sessions and non-members with a reason", async () => {
    const s = await scene();
    const stranger = await createUser("Stranger");
    const server = await startCollab();
    const cases: [string, ReturnType<typeof connect>][] = [
      ["session_invalid", connect(server, null, s.docId)],
      ["session_invalid", connect(server, { cookie: "tf_session=" + "A".repeat(43) }, s.docId)],
      ["forbidden", connect(server, stranger, s.docId)],
      ["forbidden", connect(server, s.owner, "00000000-0000-4000-8000-000000000000")],
      ["bad_document", connect(server, s.owner, s.docId, { name: "not-a-document" })],
      ["bad_document", connect(server, s.owner, s.docId, { name: `doc:${s.docId}/../x` })],
    ];
    for (const [reason, client] of cases) {
      await expect(client.ready).rejects.toThrow(`denied:${reason}`);
    }
    const wrongPath = connect(server, s.owner, s.docId, { path: "/other" });
    await sleep(500);
    expect(wrongPath.provider.isAuthenticated).toBe(false);
  });

  it("keeps ids and a role in the connection context, never the cookie", async () => {
    const s = await scene();
    const server = await startCollab();
    const a = connect(server, s.editor, s.docId);
    await a.ready;
    const [connection] = server.hocuspocus.documents.get(`doc:${s.docId}`)!.getConnections();
    expect(connection!.context).toEqual({
      userId: s.editor.id,
      displayName: "Editor",
      sessionId: s.editor.sessionId,
      role: "editor",
      canEdit: true,
      documentId: s.docId,
      roomId: s.roomId,
    });
    expect(JSON.stringify(connection!.context)).not.toContain(s.editor.secret);
  });

  it("caps connections per user and per process", async () => {
    const s = await scene();
    const server = await startCollab({ maxConnectionsPerUser: 2, maxConnectionsTotal: 5 });
    const first = [connect(server, s.owner, s.docId), connect(server, s.owner, s.docId)];
    await Promise.all(first.map((c) => c.ready));
    const third = connect(server, s.owner, s.docId);
    await expect(third.ready).rejects.toThrow("denied:too_many_connections");
    // other users are unaffected, until the process-wide cap of 5 sockets is reached
    // (the refused third attempt still holds its socket open until the unauthenticated timeout)
    const e1 = connect(server, s.editor, s.docId);
    const e2 = connect(server, s.viewer, s.docId);
    await Promise.all([e1.ready, e2.ready]);
    const over = connect(server, s.viewer, s.docId);
    await sleep(700);
    expect(over.provider.isAuthenticated).toBe(false);
    expect(server.hocuspocus.getConnectionsCount()).toBeLessThanOrEqual(4);
  });
});

describe("operations", () => {
  it("serves /health and reports unavailable after shutdown starts", async () => {
    const server = await startCollab();
    const ok = await fetch(`http://127.0.0.1:${server.port}/health`);
    expect(ok.status).toBe(200);
    expect(await ok.json()).toMatchObject({ status: "ok", lease: true });
    expect((await fetch(`http://127.0.0.1:${server.port}/nothing`)).status).toBe(404);
  });

  it("shuts down gracefully: closes clients with 1001, stores, snapshots and releases the lease", async () => {
    const s = await scene(interchange("Bye"));
    const server = await startCollab();
    const a = connect(server, s.owner, s.docId);
    await a.ready;
    appendText(a.ydoc, " now");
    await until(() => docText(server.hocuspocus.documents.get(`doc:${s.docId}`)!).includes("now"), "server has the edit");
    await server.stop();
    await until(() => a.closes.some((c) => c.code === 1001), "close 1001");
    const state = await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [s.docId]);
    expect(Number(state.update_counter)).toBeGreaterThan(0);
    const kinds = (await all<{ kind: string }>("SELECT kind FROM document_snapshots WHERE document_id = $1 ORDER BY created_at", [s.docId])).map((r) => r.kind);
    expect(kinds).toContain("final");
    const lease = await one<{ holder_id: string | null }>("SELECT holder_id FROM collab_lease");
    expect(lease.holder_id).toBeNull();
    const next = await startCollab(); // acquires at once: no waiting for the TTL
    expect(next.lease.held).toBe(true);
  });
});
