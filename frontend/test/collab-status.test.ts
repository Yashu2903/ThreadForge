import { describe, expect, it } from "vitest";
import { colorFor, displayNameFor, renderCaret } from "../src/collab/identity";
import { CLOSE, RETRY_DELAY_MS, type RawState, TERMINAL_CODES, TERMINAL_DENIALS, deriveStatus } from "../src/collab/status";

const base: RawState = { connection: "connected", synced: true, unsynced: 0, scope: "read-write", canEdit: true, accessLost: false };
const status = (over: Partial<RawState>) => deriveStatus({ ...base, ...over });

describe("sync status wording", () => {
  it("Synced only when connected, first sync done and nothing unacknowledged", () => {
    expect(status({})).toMatchObject({ kind: "synced", label: "Synced", editable: true, atRisk: 0 });
  });
  it("Syncing while the first sync is open or changes are on their way", () => {
    expect(status({ synced: false })).toMatchObject({ kind: "syncing", label: "Syncing…" });
    expect(status({ unsynced: 3 })).toMatchObject({ kind: "syncing", atRisk: 3 });
  });
  it("Offline names the number of unsynced changes", () => {
    expect(status({ connection: "disconnected", synced: false })).toMatchObject({ kind: "offline", label: "Offline" });
    expect(status({ connection: "disconnected", unsynced: 1 }).label).toBe("Offline (1 unsynced change)");
    expect(status({ connection: "disconnected", unsynced: 4 }).label).toBe("Offline (4 unsynced changes)");
    expect(status({ connection: "disconnected", unsynced: 4 }).atRisk).toBe(4);
  });
  it("Connecting at the very start is not 'Offline'", () => {
    expect(status({ connection: "connecting", synced: false })).toMatchObject({ kind: "connecting", label: "Connecting…" });
  });
  it("Read-only for a viewer or a read-only scope, and never editable", () => {
    expect(status({ canEdit: false })).toMatchObject({ kind: "read-only", label: "Read-only", editable: false });
    expect(status({ scope: "readonly" })).toMatchObject({ kind: "read-only", editable: false });
  });
  it("a demoted connection says Read-only (not Syncing) and keeps the count of rejected changes", () => {
    expect(status({ scope: "readonly", unsynced: 2 })).toMatchObject({ kind: "read-only", editable: false, atRisk: 2 });
    expect(status({ scope: "readonly", synced: false }).kind).toBe("syncing");
  });
  it("Access lost wins over everything and keeps the count of what was never received", () => {
    expect(status({ accessLost: true, unsynced: 2 })).toMatchObject({ kind: "access-lost", label: "Access lost", editable: false, atRisk: 2 });
    expect(status({ accessLost: true, connection: "disconnected" }).kind).toBe("access-lost");
  });
  it("4401 and 4403 are terminal, retryable codes are not", () => {
    expect(TERMINAL_CODES).toEqual([CLOSE.unauthorized, CLOSE.forbidden]);
    for (const code of [CLOSE.timeout, CLOSE.goingAway, CLOSE.tryAgainLater, CLOSE.messageTooBig]) expect(TERMINAL_CODES).not.toContain(code);
  });
  it("a denial reason is terminal unless the service says it is temporary", () => {
    for (const reason of ["session_invalid", "forbidden", "needs_repair"]) expect(TERMINAL_DENIALS).toContain(reason);
    for (const reason of ["unavailable", "too_many_connections", "throttled"]) expect(TERMINAL_DENIALS).not.toContain(reason);
    expect(RETRY_DELAY_MS.throttled).toBeGreaterThan(RETRY_DELAY_MS.unavailable!); // a throttled client waits longer
  });
});

describe("collaborator identity", () => {
  const members = [{ user_id: "u1", display_name: "Olivia" }];
  it("shows names from OUR member list, never text a client sent", () => {
    expect(displayNameFor("u1", members)).toBe("Olivia");
    expect(displayNameFor("u2", members)).toBe("Someone");
    expect(displayNameFor({ name: "Admin" }, members)).toBe("Someone");
    const el = renderCaret({ id: "u1", name: "<img src=x onerror=alert(1)>" } as never, members);
    expect(el.textContent).toBe("Olivia");
    expect(el.getAttribute("aria-hidden")).toBe("true");
    expect(el.querySelector("img")).toBeNull();
  });
  it("derives a stable colour from the id", () => {
    expect(colorFor("u1")).toBe(colorFor("u1"));
    expect(colorFor("u1")).toMatch(/^#[0-9a-f]{6}$/);
  });
});
