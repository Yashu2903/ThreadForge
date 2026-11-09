/**
 * Access over time, rate limits and the connection throttle. Real FastAPI + PostgreSQL + Redis, real
 * WebSocket clients. Timers are shortened (the production defaults are 15 s / 30 s).
 */
import { randomUUID } from "node:crypto";
import { createClient } from "redis";
import { afterEach, describe, expect, it } from "vitest";
import { addMember, createUser, interchange, pool, scene } from "./fixtures";
import { FaultyApi, appendText, connect, docText, sleep, startCollab, stopAll, until } from "./harness";

afterEach(stopAll);

const REDIS_URL = process.env.TEST_REDIS_URL ?? "redis://127.0.0.1:6390/15";
const fast = { accessCheckMs: 300, accessGraceMs: 1_500 };

const closedWith = (c: { closes: { code: number }[] }, code: number) => c.closes.some((x) => x.code === code);

describe("access is re-checked while a document is open", () => {
  it("a removed member's socket is closed with 4403; everyone else keeps working", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab(fast);
    const owner = connect(server, s.owner, s.docId);
    const editor = connect(server, s.editor, s.docId);
    await Promise.all([owner.ready, editor.ready]);
    await pool.query("UPDATE memberships SET state = 'removed', removed_at = now() WHERE room_id = $1 AND user_id = $2", [s.roomId, s.editor.id]);
    await until(() => closedWith(editor, 4403), "close 4403", 5_000);
    expect(editor.closes.find((c) => c.code === 4403)?.reason).toBe("not_a_member");
    // the owner is untouched and the removed editor's local text is still in THEIR page (their ydoc)
    appendText(owner.ydoc, " still here");
    await sleep(600);
    expect(closedWith(owner, 4403)).toBe(false);
    expect(docText(editor.ydoc)).toContain("Hello");
  });

  it("a revoked session is closed with 4401", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab(fast);
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    await pool.query("UPDATE sessions SET revoked_at = now() WHERE id = $1", [s.editor.sessionId]);
    await until(() => closedWith(editor, 4401), "close 4401", 5_000);
    expect(editor.closes.find((c) => c.code === 4401)?.reason).toBe("session_invalid");
  });

  it("a disabled user is closed with 4401", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab(fast);
    const viewer = connect(server, s.viewer, s.docId);
    await viewer.ready;
    await pool.query("UPDATE users SET state = 'disabled' WHERE id = $1", [s.viewer.id]);
    await until(() => closedWith(viewer, 4401), "close 4401", 5_000);
  });

  it("a demoted editor is made read-only at runtime: the socket stays open, their edits are dropped, and they are told", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab(fast);
    const owner = connect(server, s.owner, s.docId);
    const editor = connect(server, s.editor, s.docId);
    await Promise.all([owner.ready, editor.ready]);
    await pool.query("UPDATE memberships SET role = 'viewer' WHERE room_id = $1 AND user_id = $2", [s.roomId, s.editor.id]);
    await until(() => editor.stateless.some((m) => JSON.parse(m).type === "read_only"), "read_only notice", 5_000);
    expect(editor.closes).toEqual([]); // not disconnected
    appendText(editor.ydoc, " SNEAKY");
    await sleep(800);
    expect(docText(owner.ydoc)).not.toContain("SNEAKY");
    // the owner can still edit, and the demoted user still RECEIVES it
    appendText(owner.ydoc, " fine");
    await until(() => docText(editor.ydoc).includes("fine"), "downgraded user still reads live");
    // nothing sneaky reached the database either
    await sleep(500);
    const row = await pool.query("SELECT 1 FROM document_snapshots WHERE document_id = $1 AND content::text LIKE '%SNEAKY%'", [s.docId]);
    expect(row.rowCount).toBe(0);
  });

  it("a Redis room hint triggers a check within about a second, long before the timer", async () => {
    const s = await scene(interchange("Hello"));
    const prefix = `t${randomUUID().slice(0, 8)}`;
    const server = await startCollab({ accessCheckMs: 60_000, accessGraceMs: 120_000, redisUrl: REDIS_URL, redisPrefix: prefix });
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    await until(() => server.access.stats.redisReady, "redis ready");
    await sleep(300); // subscription is made after the connection exists
    await pool.query("UPDATE memberships SET state = 'removed', removed_at = now() WHERE room_id = $1 AND user_id = $2", [s.roomId, s.editor.id]);
    const publisher = createClient({ url: REDIS_URL });
    await publisher.connect();
    try {
      const started = Date.now();
      await publisher.publish(`${prefix}:room:${s.roomId}`, JSON.stringify({ hint: true }));
      await until(() => closedWith(editor, 4403), "close via hint", 5_000);
      expect(Date.now() - started).toBeLessThan(3_000);
      expect(server.access.stats.hint).toBeGreaterThanOrEqual(1);
      expect(server.access.stats.periodic).toBe(0); // the 60 s timer never fired
    } finally {
      await publisher.quit();
    }
  });

  it("hints are coalesced: a burst makes at most a couple of checks", async () => {
    const s = await scene(interchange("Hello"));
    const prefix = `t${randomUUID().slice(0, 8)}`;
    const server = await startCollab({ accessCheckMs: 60_000, accessGraceMs: 120_000, redisUrl: REDIS_URL, redisPrefix: prefix });
    const owner = connect(server, s.owner, s.docId);
    await owner.ready;
    await until(() => server.access.stats.redisReady, "redis ready");
    await sleep(300);
    const publisher = createClient({ url: REDIS_URL });
    await publisher.connect();
    try {
      for (let i = 0; i < 40; i++) await publisher.publish(`${prefix}:room:${s.roomId}`, "{}");
      await sleep(1_500);
      expect(server.access.stats.hint).toBeGreaterThanOrEqual(1);
      expect(server.access.stats.hint).toBeLessThanOrEqual(3); // 40 hints, ~1 check per second
    } finally {
      await publisher.quit();
    }
  });

  it("without Redis the timer alone still enforces access, and the service keeps running", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab({ ...fast, redisUrl: "redis://127.0.0.1:6399/0", redisPrefix: "nope" }); // nothing listens there
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    await pool.query("UPDATE memberships SET state = 'removed', removed_at = now() WHERE room_id = $1 AND user_id = $2", [s.roomId, s.editor.id]);
    await until(() => closedWith(editor, 4403), "close 4403 by the timer", 5_000);
    expect(server.access.stats.redisReady).toBe(false);
    expect(server.access.stats.periodic).toBeGreaterThan(0);
  });

  it("a failing API/database is tolerated for the grace period, then connections close with 1013", async () => {
    const s = await scene(interchange("Hello"));
    const api = new FaultyApi();
    const server = await startCollab({ accessCheckMs: 200, accessGraceMs: 1_200 }, { api });
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    api.failAccess = true;
    await sleep(600);
    expect(editor.closes).toEqual([]); // within the grace period: a blip must not disconnect anyone
    await until(() => closedWith(editor, 1013), "close 1013 after the grace period", 6_000);
    expect(editor.closes.find((c) => c.code === 1013)?.reason).toBe("access_unverifiable");
  });

  it("a recovering API within the grace period never disconnects anyone", async () => {
    const s = await scene(interchange("Hello"));
    const api = new FaultyApi();
    const server = await startCollab({ accessCheckMs: 200, accessGraceMs: 2_500 }, { api });
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    api.failAccess = true;
    await sleep(800);
    api.failAccess = false;
    await sleep(1_500);
    expect(editor.closes).toEqual([]);
    expect(server.access.stats.failures).toBeGreaterThan(0);
  });
});

describe("limits", () => {
  it("more than the allowed messages per second closes the connection with 4430", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab({ messagesPerSecond: 30 });
    const editor = connect(server, s.editor, s.docId);
    const owner = connect(server, s.owner, s.docId);
    await Promise.all([editor.ready, owner.ready]);
    for (let i = 0; i < 200; i++) appendText(editor.ydoc, "x"); // 200 updates in a burst
    await until(() => closedWith(editor, 4430), "close 4430", 5_000);
    expect(editor.closes.find((c) => c.code === 4430)?.reason).toBe("rate_limited");
    expect(closedWith(owner, 4430)).toBe(false); // only the offender
  });

  it("ordinary typing is far below the limit", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab({ messagesPerSecond: 100 });
    const editor = connect(server, s.editor, s.docId);
    await editor.ready;
    for (let i = 0; i < 20; i++) {
      appendText(editor.ydoc, "a");
      await sleep(50); // 20 keystrokes per second
    }
    await sleep(300);
    expect(editor.closes).toEqual([]);
  });

  it("connection attempts per IP are throttled, with a temporary denial the client can retry", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab({ connectsPerMinute: 3, banMinutes: 1 });
    const tries = [];
    for (let i = 0; i < 5; i++) {
      const c = connect(server, s.owner, s.docId);
      tries.push(c);
      await Promise.race([c.ready.catch(() => undefined), sleep(1_500)]);
    }
    const denied = tries.filter((c) => c.denied.includes("throttled"));
    expect(denied.length).toBeGreaterThanOrEqual(2);
    expect(tries.slice(0, 3).every((c) => c.denied.length === 0)).toBe(true);
  });

  it("a client cannot dodge the throttle by sending its own X-Forwarded-For / X-Real-IP", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab({ connectsPerMinute: 2, banMinutes: 1 });
    const tries = [];
    for (let i = 0; i < 5; i++) {
      const c = connect(server, s.owner, s.docId, { headers: { "X-Forwarded-For": `10.0.0.${i}`, "X-Real-IP": `10.1.1.${i}` } });
      tries.push(c);
      await Promise.race([c.ready.catch(() => undefined), sleep(1_500)]);
    }
    expect(tries.filter((c) => c.denied.includes("throttled")).length).toBeGreaterThanOrEqual(2);
  });

  it("behind a trusted proxy the proxy's header decides who is who", async () => {
    const s = await scene(interchange("Hello"));
    const server = await startCollab({ connectsPerMinute: 2, banMinutes: 1, trustProxyHeaders: true });
    const results = [];
    for (let i = 0; i < 4; i++) {
      const c = connect(server, s.owner, s.docId, { headers: { "X-Real-IP": `10.2.2.${i}` } }); // 4 different clients
      results.push(c);
      await Promise.race([c.ready.catch(() => undefined), sleep(1_500)]);
    }
    expect(results.every((c) => c.denied.length === 0)).toBe(true);
  });

  it("a member added later is checked too (a brand-new connection after a role change)", async () => {
    const s = await scene(interchange("Hello"));
    const later = await createUser("Later");
    const server = await startCollab(fast);
    await addMember(s.roomId, later, "editor");
    const c = connect(server, later, s.docId);
    await c.ready;
    await sleep(900); // several periodic checks pass
    expect(c.closes).toEqual([]);
  });
});
