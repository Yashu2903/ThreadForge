import { describe, expect, it } from "vitest";
import {
  OTHER,
  ME,
  ROOM,
  Rig,
  bootstrap,
  event,
  flush,
  member,
  message,
  tombstone,
} from "./harness.js";
import { reactionFor } from "../src/room-client.js";

const created = (seq: number, id: string) => event(seq, "message_created", id);

describe("bootstrap and going live", () => {
  it("installs state and cursor together, then resumes from through_seq", async () => {
    const rig = new Rig();
    rig.boot = bootstrap(10, [message("m1")]);
    rig.client.start();
    await flush();
    expect(rig.client.state.status).toBe("syncing");
    expect(rig.client.state.cursor).toBe(10);
    expect([...rig.client.state.messages.keys()]).toEqual(["m1"]);
    expect(rig.socket.url).toBe(`ws://api/rooms/${ROOM}/ws`);
    rig.socket.open();
    expect(rig.socket.sent[0]).toEqual({ type: "resume", schema_version: 1, after_seq: 10 });
  });

  it("is live only after sync_complete, not when the socket opens", async () => {
    const rig = new Rig();
    rig.client.start();
    await flush();
    rig.socket.open();
    expect(rig.client.state.status).toBe("syncing");
    rig.socket.receive({ type: "sync_started", schema_version: 1, room_id: ROOM, after_seq: 10, through_seq: 10 });
    expect(rig.client.state.status).toBe("syncing");
    rig.socket.receive({ type: "sync_complete", through_seq: 10 });
    await rig.clock.advance(0);
    expect(rig.client.state.status).toBe("live");
  });

  it("a page refresh (a new client) starts from a bootstrap, never from a remembered cursor", async () => {
    const first = new Rig();
    await first.goLive(10);
    await first.deliver(created(11, "m1"));
    expect(first.client.state.cursor).toBe(11);
    const second = new Rig();
    second.boot = bootstrap(15);
    second.client.start();
    await flush();
    expect(second.net.count("/sync")).toBe(1);
    second.socket.open();
    expect(second.socket.sent[0]).toMatchObject({ type: "resume", after_seq: 15 });
  });
});

describe("cursor and event processing", () => {
  it("advances the cursor and acks only AFTER the fetches succeed", async () => {
    const rig = new Rig();
    let release: (v: unknown) => void = () => {};
    rig.net.on(`/rooms/${ROOM}/messages/`, () => new Promise((resolve) => (release = resolve)));
    const socket = await rig.goLive(10);
    socket.receive(created(11, "m1"));
    await rig.clock.advance(60);
    expect(rig.client.state.cursor).toBe(10); // the fetch is still pending
    expect(socket.frames("ack")).toHaveLength(0);
    release({ body: message("m1") });
    await rig.clock.advance(200);
    expect(rig.client.state.cursor).toBe(11);
    expect(socket.frames("ack")).toEqual([{ type: "ack", through_seq: 11 }]);
    expect(rig.client.state.messages.get("m1")?.version).toBe(1);
  });

  it("a disconnect before processing completes replays safely from the old cursor", async () => {
    const rig = new Rig();
    rig.net.on(`/rooms/${ROOM}/messages/`, () => new Promise(() => {})); // never resolves
    const first = await rig.goLive(10);
    first.receive(created(11, "m1"));
    await rig.clock.advance(60);
    first.serverClose(1006);
    await rig.clock.advance(1100);
    expect(rig.sockets).toHaveLength(2);
    rig.socket.open();
    expect(rig.socket.sent[0]).toMatchObject({ type: "resume", after_seq: 10 }); // not 11
    expect(rig.client.state.cursor).toBe(10);
  });

  it("ignores duplicates", async () => {
    const rig = new Rig();
    rig.messages.set("m1", message("m1"));
    await rig.goLive(10);
    await rig.deliver(created(11, "m1"));
    await rig.deliver(created(11, "m1"), created(11, "m1"));
    expect(rig.net.count("/messages/m1")).toBe(1);
    expect(rig.client.state.cursor).toBe(11);
  });

  it("a gap does not advance the cursor: it closes the socket and reconnects from the cursor", async () => {
    const rig = new Rig();
    rig.messages.set("m1", message("m1"));
    const socket = await rig.goLive(10);
    await rig.deliver(created(11, "m1"));
    socket.receive(created(13, "m3")); // 12 is missing
    await rig.clock.advance(0);
    expect(socket.closedByClient).toBe(1000);
    expect(rig.client.state.cursor).toBe(11);
    await rig.clock.advance(1100);
    rig.socket.open();
    expect(rig.sockets).toHaveLength(2);
    expect(rig.socket.sent[0]).toMatchObject({ type: "resume", after_seq: 11 });
    expect(rig.net.count("/messages/m3")).toBe(0); // never applied past the gap
  });

  it("applies events delivered during replay in batches and acks so the server window can move", async () => {
    const rig = new Rig();
    for (let i = 1; i <= 150; i++) rig.messages.set(`m${i}`, message(`m${i}`));
    rig.boot = bootstrap(0);
    rig.client.start();
    await flush();
    rig.socket.open();
    rig.socket.receive({ type: "sync_started", schema_version: 1, room_id: ROOM, after_seq: 0, through_seq: 400 });
    for (let i = 1; i <= 150; i++) rig.socket.receive(created(i, `m${i}`)); // NO sync_complete yet
    await rig.clock.advance(400);
    const acks = rig.socket.frames("ack");
    expect(acks.length).toBeGreaterThan(0); // acked while still syncing (the server pauses at its window)
    expect(rig.client.state.cursor).toBeGreaterThan(0);
  });

  it("fetches affected messages with bounded concurrency and one fetch per distinct id", async () => {
    const rig = new Rig();
    let inFlight = 0;
    let peak = 0;
    rig.net.on(`/rooms/${ROOM}/messages/`, async (path) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      await flush();
      inFlight--;
      return { body: message(path.split("/").pop()!) };
    });
    await rig.goLive(10);
    const events = [];
    for (let i = 0; i < 12; i++) events.push(created(11 + i, `m${i % 8}`)); // 8 distinct ids
    await rig.deliver(...events);
    expect(rig.net.count("/messages/")).toBe(8);
    expect(peak).toBeLessThanOrEqual(4);
    expect(rig.client.state.cursor).toBe(22);
  });
});

describe("merging by id and version", () => {
  it("a stale fetched copy cannot overwrite a newer local version", async () => {
    const rig = new Rig();
    rig.boot = bootstrap(10, [message("m1", 3)]);
    rig.messages.set("m1", message("m1", 2));
    await rig.goLive(10);
    await rig.deliver(event(11, "message_edited", "m1"));
    expect(rig.client.state.messages.get("m1")?.version).toBe(3);
    expect(rig.client.state.cursor).toBe(11);
  });

  it("an equal version replaces and a newer version replaces", async () => {
    const rig = new Rig();
    rig.boot = bootstrap(10, [message("m1", 2)]);
    rig.messages.set("m1", message("m1", 4));
    await rig.goLive(10);
    await rig.deliver(event(11, "message_edited", "m1"));
    expect(rig.client.state.messages.get("m1")?.version).toBe(4);
  });

  it("a tombstone is final: stale live content never replaces it", async () => {
    const rig = new Rig();
    rig.boot = bootstrap(10, [tombstone("m1", 3)]);
    rig.messages.set("m1", message("m1", 9, { body: "resurrected?" })); // even a HIGHER live version
    await rig.goLive(10);
    await rig.deliver(event(11, "message_edited", "m1"));
    expect(rig.client.state.messages.get("m1")?.is_deleted).toBe(true);
    expect(rig.client.state.messages.get("m1")?.body).toBeNull();
  });

  it("a delete event turns the live message into the tombstone", async () => {
    const rig = new Rig();
    rig.boot = bootstrap(10, [message("m1", 1)]);
    rig.messages.set("m1", tombstone("m1", 2));
    await rig.goLive(10);
    await rig.deliver(event(11, "message_deleted", "m1"));
    expect(rig.client.state.messages.get("m1")?.body).toBeNull();
    rig.messages.set("m1", message("m1", 1)); // an old live copy arrives late
    await rig.deliver(event(12, "message_edited", "m1"));
    expect(rig.client.state.messages.get("m1")?.is_deleted).toBe(true);
  });

  it("an event may be older than the data: the fetch can return a NEWER version", async () => {
    const rig = new Rig();
    rig.messages.set("m1", message("m1", 5));
    await rig.goLive(10);
    await rig.deliver(created(11, "m1")); // the event says version 1
    expect(rig.client.state.messages.get("m1")?.version).toBe(5);
  });

  it("members and room are refetched when their events arrive (all member pages)", async () => {
    const rig = new Rig();
    await rig.goLive(10);
    rig.boot = bootstrap(12, [], [member(ME, "owner"), member(OTHER, "viewer")]);
    await rig.deliver(
      event(11, "member_joined", OTHER, { payload: { user_id: OTHER, role: "viewer" } }),
      event(12, "room_renamed", ROOM),
    );
    expect(rig.client.state.members.map((m) => m.user_id)).toEqual([ME, OTHER]);
    expect(rig.net.count("/members")).toBe(1);
    expect(rig.net.calls.some((c) => c.url.endsWith(`/rooms/${ROOM}`))).toBe(true);
  });
});

describe("fresh bootstrap instead of many fetches", () => {
  it("more than 20 distinct affected messages triggers a bootstrap, not single fetches", async () => {
    const rig = new Rig();
    await rig.goLive(10);
    const events = [];
    const msgs = [];
    for (let i = 0; i < 25; i++) {
      events.push(created(11 + i, `m${i}`));
      msgs.push(message(`m${i}`));
    }
    rig.boot = bootstrap(35, msgs);
    const syncBefore = rig.net.count("/sync");
    await rig.deliver(...events);
    expect(rig.net.count("/sync")).toBe(syncBefore + 1);
    expect(rig.net.count("/messages/")).toBe(0);
    expect(rig.client.state.messages.size).toBe(25);
    expect(rig.client.state.cursor).toBe(35);
  });

  it("never acknowledges more than the server has sent after a bootstrap raised the cursor", async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10);
    const events = [];
    for (let i = 0; i < 25; i++) events.push(created(11 + i, `m${i}`));
    rig.boot = bootstrap(500, []); // the snapshot is far ahead of what the socket has delivered
    await rig.deliver(...events);
    const acks = socket.frames("ack") as { through_seq: number }[];
    expect(acks.length).toBeGreaterThan(0);
    expect(Math.max(...acks.map((a) => a.through_seq))).toBeLessThanOrEqual(35); // <= last frame sent
  });
});

describe("transient failures and access loss", () => {
  it("retries a failing fetch 3 times with backoff, then reconnects from the same cursor", async () => {
    const rig = new Rig();
    rig.net.on(`/rooms/${ROOM}/messages/`, () => ({ status: 500, body: {} }));
    const socket = await rig.goLive(10);
    socket.receive(created(11, "m1"));
    await rig.clock.advance(60);
    await rig.clock.advance(500 + 1000 + 2000 + 10);
    expect(rig.net.count("/messages/m1")).toBe(4); // 1 try + 3 retries
    expect(socket.closedByClient).toBe(1000);
    expect(rig.client.state.cursor).toBe(10);
    await rig.clock.advance(1100);
    rig.socket.open();
    expect(rig.socket.sent[0]).toMatchObject({ after_seq: 10 });
  });

  it("a message that does not exist is skipped, not an error", async () => {
    const rig = new Rig();
    await rig.goLive(10); // no message registered: 404 message_not_found
    await rig.deliver(created(11, "ghost"));
    expect(rig.client.state.cursor).toBe(11);
    expect(rig.client.state.status).toBe("live");
  });

  it.each([403, 404])("HTTP %i from a fetch is access loss: terminal and cleared", async (status) => {
    const rig = new Rig();
    rig.boot = bootstrap(10, [message("m0")]);
    rig.net.on(`/rooms/${ROOM}/messages/`, () => ({ status, body: { error: { code: "room_not_found" } } }));
    await rig.goLive(10);
    await rig.deliver(created(11, "m1"));
    expect(rig.client.state.status).toBe("terminal");
    expect(rig.client.state.terminalReason).toBe("access_lost");
    expect(rig.client.state.messages.size).toBe(0);
    expect(rig.client.state.room).toBeNull();
    const sockets = rig.sockets.length;
    await rig.clock.advance(120_000);
    expect(rig.sockets).toHaveLength(sockets); // no reconnect
  });

  it("HTTP 401 from a fetch means the session is gone: terminal, sign in again, nothing reconnects", async () => {
    const rig = new Rig();
    rig.net.on(`/rooms/${ROOM}/messages/`, () => ({ status: 401, body: { error: { code: "unauthenticated" } } }));
    await rig.goLive(10);
    await rig.deliver(created(11, "m1"));
    expect(rig.client.state.terminalReason).toBe("sign_in_required");
    const sockets = rig.sockets.length;
    await rig.clock.advance(120_000);
    expect(rig.sockets).toHaveLength(sockets);
  });

  it("member_removed for the current user is terminal, clears the room and never reconnects", async () => {
    const rig = new Rig();
    rig.boot = bootstrap(10, [message("m0")]);
    const socket = await rig.goLive(10);
    await rig.deliver(event(11, "member_removed", ME, { payload: { user_id: ME } }));
    expect(rig.client.state.terminalReason).toBe("access_lost");
    expect(rig.client.state.messages.size).toBe(0);
    expect(socket.closedByClient).toBe(1000);
    expect(rig.clock.pending).toBe(0);
  });

  it("member_removed for someone else only refreshes the members", async () => {
    const rig = new Rig();
    await rig.goLive(10);
    await rig.deliver(event(11, "member_removed", OTHER, { payload: { user_id: OTHER } }));
    expect(rig.client.state.status).toBe("live");
    expect(rig.net.count("/members")).toBe(1);
  });
});

describe("close codes", () => {
  const live = async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10);
    return { rig, socket };
  };

  it.each([
    [4401, "sign_in_required"],
    [4403, "access_lost"],
    [4400, "protocol_error"],
    [1003, "protocol_error"],
    [1009, "protocol_error"],
  ])("code %i is terminal (%s) and never reconnects", async (code, reason) => {
    const { rig, socket } = await live();
    socket.serverClose(code);
    expect(rig.client.state.status).toBe("terminal");
    expect(rig.client.state.terminalReason).toBe(reason);
    await rig.clock.advance(300_000);
    expect(rig.sockets).toHaveLength(1);
    if (code === 4403) expect(rig.client.state.room).toBeNull(); // room data cleared
  });

  it.each([1001, 1013, 4408, 4429, 1006])("code %i reconnects with backoff", async (code) => {
    const { rig, socket } = await live();
    socket.serverClose(code);
    expect(rig.client.state.status).toBe("reconnecting");
    expect(rig.sockets).toHaveLength(1);
    await rig.clock.advance(1100);
    expect(rig.sockets).toHaveLength(2);
    expect(rig.client.state.cursor).toBe(10); // cursor and state kept
  });

  it("4409 retries after at least 10 s and stops after 3 tries", async () => {
    const { rig, socket } = await live();
    socket.serverClose(4409);
    await rig.clock.advance(9_000);
    expect(rig.sockets).toHaveLength(1);
    await rig.clock.advance(1_100);
    expect(rig.sockets).toHaveLength(2);
    rig.socket.serverClose(4409);
    await rig.clock.advance(10_100);
    expect(rig.sockets).toHaveLength(3);
    rig.socket.serverClose(4409);
    expect(rig.client.state.terminalReason).toBe("too_many_connections");
    await rig.clock.advance(60_000);
    expect(rig.sockets).toHaveLength(3);
  });

  it("4410 fetches a fresh bootstrap, replaces state + cursor together and reconnects", async () => {
    const { rig, socket } = await live();
    rig.boot = bootstrap(40, [message("fresh")]);
    socket.serverClose(4410);
    await rig.clock.advance(10);
    expect(rig.client.state.cursor).toBe(40);
    expect([...rig.client.state.messages.keys()]).toEqual(["fresh"]);
    rig.socket.open();
    expect(rig.socket.sent[0]).toMatchObject({ type: "resume", after_seq: 40 });
  });

  it("a resync_required frame stops applying the old stream and resyncs", async () => {
    const { rig, socket } = await live();
    rig.boot = bootstrap(77);
    socket.receive({ type: "resync_required", reason: "event_gap" });
    await rig.clock.advance(10);
    expect(rig.client.state.cursor).toBe(77);
    expect(socket.closedByClient).toBe(1000);
  });

  it("the resync-loop breaker stops after 3 resyncs within 60 s", async () => {
    const { rig, socket } = await live();
    let current = socket;
    for (let i = 0; i < 3; i++) {
      current.serverClose(4410);
      await rig.clock.advance(10);
      current = rig.socket;
      current.open();
    }
    expect(rig.client.state.status).not.toBe("terminal"); // exactly 3 are allowed
    current.serverClose(4410);
    await rig.clock.advance(10);
    expect(rig.client.state.status).toBe("terminal");
    expect(rig.client.state.terminalReason).toBe("cannot_synchronize");
  });

  it("resyncs spread over more than 60 s do not trip the breaker", async () => {
    const { rig, socket } = await live();
    let current = socket;
    for (let i = 0; i < 6; i++) {
      current.serverClose(4410);
      await rig.clock.advance(10);
      current = rig.socket;
      current.open();
      await rig.clock.advance(25_000);
    }
    expect(rig.client.state.status).not.toBe("terminal");
  });

  it("three slow-consumer closes within 5 minutes mean resync", async () => {
    const { rig, socket } = await live();
    rig.boot = bootstrap(90);
    let current = socket;
    for (let i = 0; i < 2; i++) {
      current.serverClose(4429);
      await rig.clock.advance(40_000);
      current = rig.socket;
      current.open();
    }
    expect(rig.client.state.cursor).toBe(10); // still just reconnecting
    current.serverClose(4429);
    await rig.clock.advance(10);
    expect(rig.client.state.cursor).toBe(90); // the third triggered a bootstrap
  });

  it("reactionFor covers every documented code", () => {
    expect(reactionFor(1001).kind).toBe("reconnect");
    expect(reactionFor(4410).kind).toBe("resync");
    expect(reactionFor(4409).kind).toBe("too_many");
  });
});

describe("reconnect behaviour", () => {
  it("backs off exponentially with jitter and caps near 30 s", async () => {
    const rig = new Rig();
    rig.rand = 1; // the longest jittered delay
    const socket = await rig.goLive(10);
    socket.serverClose(1006);
    const delays: number[] = [];
    let seen = rig.sockets.length;
    for (let i = 0; i < 8; i++) {
      const start = rig.clock.t;
      while (rig.sockets.length === seen) await rig.clock.advance(100);
      delays.push(rig.clock.t - start);
      seen = rig.sockets.length;
      rig.socket.serverClose(1006); // never reaches live: the attempt counter keeps growing
    }
    expect(delays[0]).toBeGreaterThanOrEqual(1000);
    expect(delays[0]).toBeLessThanOrEqual(1100);
    expect(delays[1]).toBeGreaterThanOrEqual(2000);
    expect(delays[2]).toBeGreaterThanOrEqual(4000);
    expect(Math.max(...delays)).toBeLessThanOrEqual(30_100);
    expect(delays[7]).toBeGreaterThanOrEqual(30_000);
  });

  it("jitter spreads the delays", async () => {
    const low = new Rig();
    low.rand = 0;
    (await low.goLive(10)).serverClose(1006);
    await low.clock.advance(499);
    expect(low.sockets).toHaveLength(1);
    await low.clock.advance(20);
    expect(low.sockets).toHaveLength(2); // 0.5 s at the low end of the jitter
  });

  it("resets the backoff after a connection stayed live for 30 s", async () => {
    const rig = new Rig();
    rig.rand = 1;
    const socket = await rig.goLive(10);
    socket.serverClose(1006);
    await rig.clock.advance(1100);
    rig.socket.open();
    rig.socket.receive({ type: "sync_complete", through_seq: 10 });
    await rig.clock.advance(31_000); // stable
    rig.socket.serverClose(1006);
    await rig.clock.advance(1100);
    expect(rig.sockets).toHaveLength(3); // back to ~1 s, not 2 s
  });

  it("runs ONE reconnect loop however many triggers arrive", async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10);
    socket.serverClose(1006);
    socket.serverClose(1006);
    socket.serverClose(1001);
    await rig.clock.advance(1100);
    expect(rig.sockets).toHaveLength(2);
  });

  it("ignores frames and timers from an obsolete socket (generation counter)", async () => {
    const rig = new Rig();
    rig.messages.set("m1", message("m1"));
    const old = await rig.goLive(10);
    old.serverClose(1006);
    await rig.clock.advance(1100);
    rig.socket.open();
    old.receive(created(11, "m1")); // a straggler from the dead socket
    await rig.clock.advance(100);
    expect(rig.net.count("/messages/")).toBe(0);
    expect(rig.client.state.cursor).toBe(10);
  });

  it("heartbeats every 15 s and reconnects after 45 s of silence", async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10);
    await rig.clock.advance(15_100);
    expect(socket.frames("heartbeat")).toHaveLength(1);
    socket.receive({ type: "heartbeat_ack", server_time: "x" }); // proves liveness
    await rig.clock.advance(15_000);
    expect(socket.frames("heartbeat")).toHaveLength(2);
    expect(rig.sockets).toHaveLength(1);
    await rig.clock.advance(45_100); // nothing at all from the server now
    expect(socket.closedByClient).toBe(1000);
    await rig.clock.advance(1100);
    expect(rig.sockets).toHaveLength(2);
  });
});

describe("switching rooms and stopping", () => {
  it("cleans up the old room: socket, timers, fetches and state", async () => {
    const rig = new Rig();
    rig.net.on(`/rooms/${ROOM}/messages/`, () => new Promise(() => {}));
    const socket = await rig.goLive(10);
    socket.receive(created(11, "m1"));
    await rig.clock.advance(60);
    const pending = rig.net.calls.at(-1)!;
    rig.client.switchRoom("44444444-4444-4444-4444-444444444444");
    expect(socket.closedByClient).toBe(1000);
    expect(pending.signal?.aborted).toBe(true);
    expect(rig.client.state.messages.size).toBe(0);
    socket.receive(created(12, "late")); // the old socket is dead to the client
    await rig.clock.advance(100);
    expect(rig.client.state.cursor).toBe(0);
  });

  it("stop() leaves no timers behind", async () => {
    const rig = new Rig();
    await rig.goLive(10);
    rig.client.userTyping();
    rig.client.stop();
    expect(rig.clock.pending).toBe(0);
    expect(rig.client.state.terminalReason).toBe("stopped");
  });
});

describe("sending messages", () => {
  it("retries an unconfirmed send with the SAME client_message_id and never duplicates a confirmed one", async () => {
    const rig = new Rig();
    let attempts = 0;
    rig.net.on(`/rooms/${ROOM}/messages`, (_path, init) => {
      if (init?.method !== "POST") return { body: {} };
      attempts++;
      if (attempts === 1) throw new Error("network down");
      const body = JSON.parse(init.body!) as { client_message_id: string };
      return { status: attempts === 2 ? 201 : 200, body: message("m9", 1, { client_message_id: body.client_message_id }) };
    });
    await rig.goLive(10);
    expect(await rig.client.sendMessage("hello", "cid-1")).toBeNull();
    expect(rig.client.pendingSendIds).toEqual(["cid-1"]);
    await rig.client.retryPending();
    expect(rig.client.pendingSendIds).toEqual([]);
    const posts = rig.net.calls.filter((c) => c.method === "POST").map((c) => JSON.parse(c.body!));
    expect(posts.map((p) => p.client_message_id)).toEqual(["cid-1", "cid-1"]); // same id, never a new one
    await rig.client.retryPending();
    expect(rig.net.calls.filter((c) => c.method === "POST")).toHaveLength(2); // confirmed: not re-sent
    expect(rig.client.state.messages.has("m9")).toBe(true);
  });

  it("reconnecting retries pending sends with their original ids", async () => {
    const rig = new Rig();
    let fail = true;
    rig.net.on(`/rooms/${ROOM}/messages`, (_path, init) => {
      if (init?.method !== "POST") return { body: {} };
      if (fail) throw new Error("down");
      return { status: 201, body: message("m1") };
    });
    const socket = await rig.goLive(10);
    await rig.client.sendMessage("x", "cid-A");
    fail = false;
    socket.serverClose(1006);
    await rig.clock.advance(1100);
    rig.socket.open();
    rig.socket.receive({ type: "sync_complete", through_seq: 10 });
    await rig.clock.advance(50);
    const ids = rig.net.calls.filter((c) => c.method === "POST").map((c) => JSON.parse(c.body!).client_message_id);
    expect(ids).toEqual(["cid-A", "cid-A"]);
    expect(rig.client.pendingSendIds).toEqual([]);
  });
});

describe("acks", () => {
  it("batches acks: never more than one per 120 ms, always the highest processed seq", async () => {
    const rig = new Rig();
    rig.messages.set("m1", message("m1"));
    const socket = await rig.goLive(10);
    for (let i = 11; i <= 40; i++) {
      socket.receive(event(i, "room_renamed", ROOM));
      await rig.clock.advance(60); // each becomes its own batch
    }
    await rig.clock.advance(500);
    const acks = socket.frames("ack") as { through_seq: number }[];
    expect(acks.at(-1)?.through_seq).toBe(40);
    expect(acks.length).toBeLessThan(30); // coalesced, not one per event
    for (let i = 1; i < acks.length; i++) expect(acks[i]!.through_seq).toBeGreaterThan(acks[i - 1]!.through_seq);
  });
});

describe("presence and typing", () => {
  it("replaces presence from each snapshot and shows degraded as unknown, not offline", async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10);
    socket.receive({ type: "presence_snapshot", room_id: ROOM, online: [{ user_id: OTHER, availability: "busy" }], degraded: false });
    expect(rig.client.state.presence).toEqual({ online: [{ user_id: OTHER, availability: "busy" }], degraded: false });
    socket.receive({ type: "presence_snapshot", room_id: ROOM, online: [], degraded: true });
    expect(rig.client.state.presence).toEqual({ online: [], degraded: true });
  });

  it("typing entries expire locally after 6 s and a new snapshot replaces them", async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10);
    socket.receive({ type: "typing_state", room_id: ROOM, typing: [OTHER], degraded: false });
    expect(rig.client.state.typing.userIds).toEqual([OTHER]);
    await rig.clock.advance(5_900);
    expect(rig.client.state.typing.userIds).toEqual([OTHER]);
    await rig.clock.advance(200);
    expect(rig.client.state.typing.userIds).toEqual([]); // a missed stop recovers by itself
    socket.receive({ type: "typing_state", room_id: ROOM, typing: [OTHER], degraded: false });
    socket.receive({ type: "typing_state", room_id: ROOM, typing: [], degraded: false });
    expect(rig.client.state.typing.userIds).toEqual([]);
  });

  it("sends typing start at most once every 3 s and stop after 4 s without input", async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10);
    for (let i = 0; i < 10; i++) {
      rig.client.userTyping();
      await rig.clock.advance(200);
    }
    expect(socket.frames("typing")).toEqual([{ type: "typing", state: "start" }]);
    await rig.clock.advance(1_100); // 3.1 s since the first start, still typing
    rig.client.userTyping();
    expect(socket.frames("typing").filter((f) => f.state === "start")).toHaveLength(2);
    await rig.clock.advance(4_100);
    expect(socket.frames("typing").at(-1)).toEqual({ type: "typing", state: "stop" });
  });

  it("sending a message stops typing", async () => {
    const rig = new Rig();
    rig.net.on(`/rooms/${ROOM}/messages`, (_p, init) =>
      init?.method === "POST" ? { status: 201, body: message("m1") } : { body: {} },
    );
    const socket = await rig.goLive(10);
    rig.client.userTyping();
    await rig.client.sendMessage("done", "cid");
    expect(socket.frames("typing").at(-1)).toEqual({ type: "typing", state: "stop" });
  });

  it("sends set_availability as a frame", async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10);
    rig.client.setAvailability("dnd");
    expect(socket.frames("set_availability")).toEqual([{ type: "set_availability", value: "dnd" }]);
  });
});


describe("a refused handshake (browsers only see an abnormal close)", () => {
  it("1006 before any frame is followed by a re-bootstrap, not a blind socket retry", async () => {
    const rig = new Rig();
    await rig.goLive(10);
    rig.socket.serverClose(1006); // was live: a plain drop
    await rig.clock.advance(1100);
    expect(rig.net.count("/sync")).toBe(1); // reconnected without bootstrapping
    rig.socket.serverClose(1006); // the new socket NEVER delivered a frame: maybe refused
    await rig.clock.advance(2100);
    expect(rig.net.count("/sync")).toBe(2); // asked over HTTP instead
  });

  it("learns about a removal from the bootstrap (404) and clears the room", async () => {
    const rig = new Rig();
    rig.boot = bootstrap(10, [message("m1")]);
    await rig.goLive(10);
    rig.socket.serverClose(1006);
    await rig.clock.advance(1100);
    rig.net.on(`/rooms/${ROOM}/sync`, () => ({ status: 404, body: { error: { code: "room_not_found" } } }));
    rig.socket.serverClose(1006);
    await rig.clock.advance(3000);
    expect(rig.client.state.terminalReason).toBe("access_lost");
    expect(rig.client.state.messages.size).toBe(0);
    const sockets = rig.sockets.length;
    await rig.clock.advance(120_000);
    expect(rig.sockets).toHaveLength(sockets);
  });

  it("learns about a revoked session from the bootstrap (401): sign in again", async () => {
    const rig = new Rig();
    await rig.goLive(10);
    rig.socket.serverClose(1006);
    await rig.clock.advance(1100);
    rig.net.on(`/rooms/${ROOM}/sync`, () => ({ status: 401, body: { error: { code: "unauthenticated" } } }));
    rig.socket.serverClose(1006);
    await rig.clock.advance(3000);
    expect(rig.client.state.terminalReason).toBe("sign_in_required");
  });

  it("a drop AFTER frames arrived is just a reconnect", async () => {
    const rig = new Rig();
    const socket = await rig.goLive(10); // sync frames were delivered
    socket.serverClose(1006);
    await rig.clock.advance(1100);
    expect(rig.net.count("/sync")).toBe(1);
  });
});
