/** AI phase 1: how the room client folds `ai_request_updated`, `source_updated` and
 *  `room_ai_settings_changed` into signals, and when the cursor moves. */
import { describe, expect, it } from "vitest";
import { Rig, bootstrap, event } from "./harness.js";
import type { AiSignals } from "../src/protocol.js";

const R1 = "a1111111-1111-4111-8111-111111111111";
const R2 = "a2222222-2222-4222-8222-222222222222";
const S1 = "b1111111-1111-4111-8111-111111111111";
const V1 = "c1111111-1111-4111-8111-111111111111";
const V2 = "c2222222-2222-4222-8222-222222222222";

const requestEvent = (seq: number, id: string, state: string) =>
  event(seq, "ai_request_updated", id, { resource_type: "ai_request", payload: { request_id: id, state } });
const sourceEvent = (seq: number, version: string, state: string) =>
  event(seq, "source_updated", S1, { resource_type: "source", payload: { source_id: S1, source_version_id: version, state } });
const P1 = "d1111111-1111-4111-8111-111111111111";
const proposalEvent = (seq: number, id: string, state: unknown) =>
  event(seq, "ai_proposal_updated", id, { resource_type: "ai_proposal", payload: { proposal_id: id, state } });
const settingsEvent = (seq: number, enabled: boolean) =>
  event(seq, "room_ai_settings_changed", "room-1", { payload: { ai_enabled: enabled } });

function rigWithHandler(options: { fail?: () => boolean } = {}) {
  const calls: AiSignals[] = [];
  const rig = new Rig({
    onAiSignals: async (signals) => {
      calls.push(signals);
      if (options.fail?.()) throw new Error("handler failed");
    },
  });
  return { rig, calls };
}

describe("AI signals", () => {
  it("keeps the last state per request and per source in a batch", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    calls.length = 0;
    await rig.deliver(
      requestEvent(11, R1, "queued"),
      requestEvent(12, R1, "running"),
      requestEvent(13, R2, "waiting_for_sources"),
      requestEvent(14, R1, "succeeded"),
      sourceEvent(15, V1, "created"),
      sourceEvent(16, V2, "ready"),
    );
    expect(calls).toHaveLength(1);
    const s = calls[0]!;
    expect([...s.requests]).toEqual([
      [R1, "succeeded"],
      [R2, "waiting_for_sources"],
    ]);
    expect(s.sources.get(S1)).toEqual({ versionId: V2, state: "ready" });
    expect(s.aiEnabled).toBeNull();
    expect(s.reset).toBe(false);
    expect(rig.client.state.cursor).toBe(16);
  });

  it("keeps the last state per proposal, ignores a malformed proposal event, and moves the cursor", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    calls.length = 0;
    await rig.deliver(proposalEvent(11, P1, "pending"), proposalEvent(12, P1, "applying"), proposalEvent(13, P1, "applied"), proposalEvent(14, "d2", 5));
    expect(calls).toHaveLength(1);
    expect([...calls[0]!.proposals]).toEqual([[P1, "applied"]]);
    expect(rig.client.state.cursor).toBe(14);
  });

  it("reports the owner's room switch, last value wins", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    calls.length = 0;
    await rig.deliver(settingsEvent(11, false), settingsEvent(12, true), settingsEvent(13, false));
    expect(calls.at(-1)!.aiEnabled).toBe(false);
  });

  it("ignores AI events with malformed payloads instead of crashing", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    calls.length = 0;
    await rig.deliver(
      event(11, "ai_request_updated", R1, { resource_type: "ai_request", payload: { request_id: 5, state: null } }),
      event(12, "source_updated", S1, { resource_type: "source", payload: {} }),
      event(13, "room_ai_settings_changed", "room-1", { payload: { ai_enabled: "yes" } }),
    );
    expect(calls).toHaveLength(0); // nothing to report
    expect(rig.client.state.cursor).toBe(13); // but the cursor still moved past them
  });

  it("does not advance the cursor or ack until the application handler succeeded", async () => {
    let failing = true;
    const { rig, calls } = rigWithHandler({ fail: () => failing });
    await rig.goLive(10);
    rig.socket.receive(requestEvent(11, R1, "succeeded"));
    await rig.clock.advance(60);
    expect(rig.client.state.cursor).toBe(10);
    expect(rig.socket.frames("ack")).toEqual([]);
    failing = false;
    await rig.clock.advance(5_000);
    expect(calls.length).toBeGreaterThan(1);
    expect(rig.client.state.cursor).toBe(11);
  });

  it("tells the app once, after going live, that a bootstrap replaced its state", async () => {
    const { rig, calls } = rigWithHandler();
    await rig.goLive(10);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ reset: true });
    expect(calls[0]!.requests.size).toBe(0);
    await rig.deliver(requestEvent(11, R1, "queued"));
    expect(calls.at(-1)!.reset).toBe(false);
  });

  it("is not required: a client without the handler just moves its cursor", async () => {
    const rig = new Rig({});
    await rig.goLive(10);
    await rig.deliver(requestEvent(11, R1, "queued"), sourceEvent(12, V1, "ready"));
    expect(rig.client.state.cursor).toBe(12);
  });

  it("passes the message kind through, so an AI answer renders differently from chat", async () => {
    const rig = new Rig({});
    rig.boot = bootstrap(10, [
      {
        id: "m1", room_id: "r", author_id: "00000000-0000-4000-8000-0000000a1a1a", author_display_name: "ThreadForge AI",
        client_message_id: "c1", body: "Answer", created_at: "2026-01-01T00:00:00Z", edited_at: null, deleted_at: null,
        deleted_by: null, version: 1, is_deleted: false, kind: "ai_answer", ai_request_id: R1,
      },
    ]);
    await rig.goLive(10);
    expect(rig.client.state.messages.get("m1")).toMatchObject({ kind: "ai_answer", ai_request_id: R1 });
  });
});
