/** Part 6: how the room client folds document events into signals, and when the cursor moves. */
import { describe, expect, it } from "vitest";
import { Rig, bootstrap, event, flush } from "./harness.js";
import type { DocumentSignals } from "../src/protocol.js";

const D1 = "d1111111-1111-4111-8111-111111111111";
const D2 = "d2222222-2222-4222-8222-222222222222";

const docEvent = (seq: number, type: string, id: string, extra: Record<string, unknown> = {}) =>
  event(seq, type, id, {
    payload: type === "active_document_changed" ? { document_id: id, selection_version: extra.sv } : { document_id: id },
    resource_version: type === "document_content_updated" ? (extra.v as number) : null,
  });

function rigWithHandler(options: { fail?: () => boolean } = {}) {
  const calls: DocumentSignals[] = [];
  const rig = new Rig({
    onDocumentSignals: async (signals) => {
      calls.push(signals);
      if (options.fail?.()) throw new Error("handler failed");
    },
  });
  return { rig, calls };
}

describe("document signals", () => {
  it("coalesces events per document and reports the highest content version", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    calls.length = 0;
    await rig.deliver(
      docEvent(11, "document_content_updated", D1, { v: 3 }),
      docEvent(12, "document_content_updated", D1, { v: 5 }),
      docEvent(13, "document_content_updated", D1, { v: 4 }),
      docEvent(14, "document_renamed", D1),
      docEvent(15, "document_created", D2),
    );
    expect(calls).toHaveLength(1);
    const signals = calls[0]!;
    expect(signals.listChanged).toBe(true);
    expect([...signals.documents.keys()].sort()).toEqual([D1, D2]);
    expect(signals.documents.get(D1)).toEqual({ created: false, renamed: true, contentVersion: 5 });
    expect(signals.documents.get(D2)).toEqual({ created: true, renamed: false, contentVersion: null });
    expect(rig.client.state.cursor).toBe(15);
  });

  it("applies the newest selection and never moves it backwards", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    await rig.deliver(docEvent(11, "active_document_changed", D1, { sv: 2 }));
    expect(rig.client.state.room?.active_document_id).toBe(D1);
    expect(rig.client.state.room?.active_document_version).toBe(2);
    await rig.deliver(docEvent(12, "active_document_changed", D2, { sv: 1 })); // stale announcement
    expect(rig.client.state.room?.active_document_id).toBe(D1);
    // within one batch the highest selection_version wins whatever the order
    await rig.deliver(docEvent(13, "active_document_changed", D2, { sv: 4 }), docEvent(14, "active_document_changed", D1, { sv: 3 }));
    expect(rig.client.state.room?.active_document_id).toBe(D2);
    expect(rig.client.state.room?.active_document_version).toBe(4);
    expect(calls.at(-1)!.active).toEqual({ documentId: D2, selectionVersion: 4 });
  });

  it("does not advance the cursor or ack until the application handler succeeded", async () => {
    let failing = true;
    const { rig, calls } = rigWithHandler({ fail: () => failing });
    await rig.goLive(10);
    for (const e of [docEvent(11, "document_content_updated", D1, { v: 2 })]) rig.socket.receive(e);
    await rig.clock.advance(60);
    expect(rig.client.state.cursor).toBe(10); // handler is retrying: nothing moved
    expect(rig.socket.frames("ack")).toEqual([]);
    failing = false;
    await rig.clock.advance(5_000); // the retry backoff elapses and the handler now succeeds
    expect(calls.length).toBeGreaterThan(1);
    expect(rig.client.state.cursor).toBe(11);
    await rig.clock.advance(200);
    expect(rig.socket.frames("ack").at(-1)).toMatchObject({ through_seq: 11 });
  });

  it("replays the batch after a reconnect when the handler keeps failing", async () => {
    const { rig } = rigWithHandler({ fail: () => true });
    await rig.goLive(10);
    rig.socket.receive(docEvent(11, "document_content_updated", D1, { v: 2 }));
    await rig.clock.advance(60_000);
    expect(rig.client.state.cursor).toBe(10); // never advanced past an unhandled event
    expect(rig.sockets.length).toBeGreaterThan(1); // it gave up on the socket and reconnects
    rig.sockets.at(-1)!.open();
    expect(rig.sockets.at(-1)!.sent[0]).toMatchObject({ type: "resume", after_seq: 10 });
  });

  it("tells the app once, after going live, that a bootstrap replaced its state", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ reset: true, listChanged: false });
    expect(calls[0]!.documents.size).toBe(0);
    await rig.deliver(docEvent(11, "document_renamed", D1));
    expect(calls.at(-1)!.reset).toBe(false); // an ordinary batch is not a reset
  });

  it("reports reset when many affected messages force a fresh bootstrap mid-stream", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    calls.length = 0;
    rig.boot = bootstrap(40);
    rig.boot = { ...rig.boot, room: { ...rig.boot.room, active_document_id: D2, active_document_version: 7 } };
    const events = Array.from({ length: 25 }, (_, i) => event(11 + i, "message_created", `m${i}`));
    await rig.deliver(...events, docEvent(36, "document_renamed", D1));
    expect(rig.client.state.room?.active_document_id).toBe(D2);
    expect(calls.some((c) => c.reset)).toBe(true);
  });

  it("works without a handler (the chat-only client of Part 5)", async () => {
    const rig = new Rig();
    await rig.goLive(10);
    await rig.deliver(docEvent(11, "document_created", D1));
    expect(rig.client.state.cursor).toBe(11);
    await flush();
  });

  it("a room refetch after a rename cannot move the selection backwards", async () => {
    const { rig } = rigWithHandler();
    await rig.goLive(10);
    await rig.deliver(docEvent(11, "active_document_changed", D2, { sv: 5 }));
    // the refetch answers from before that change (a copy: state.room holds the bootstrap object)
    rig.boot = { ...rig.boot, room: { ...rig.boot.room, active_document_id: D1, active_document_version: 4 } };
    await rig.deliver(event(12, "room_renamed", ROOM_ID));
    expect(rig.client.state.room?.active_document_id).toBe(D2);
    expect(rig.client.state.room?.active_document_version).toBe(5);
  });
});

const ROOM_ID = "11111111-1111-1111-1111-111111111111";
