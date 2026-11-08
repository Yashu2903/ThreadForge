import { describe, expect, it } from "vitest";
import type { DocumentSignals } from "threadforge-client";
import { DocumentStore, REFETCH_MIN_INTERVAL_MS, type StoreDeps } from "../src/documents/store";
import { ROOM, flush, fakeApi, full, summary } from "./helpers";

class Clock implements StoreDeps {
  t = 10_000;
  timers: { at: number; fn: () => void }[] = [];
  now = () => this.t;
  setTimeout = (fn: () => void, ms: number) => {
    const timer = { at: this.t + ms, fn };
    this.timers.push(timer);
    return timer;
  };
  clearTimeout = (h: unknown) => {
    this.timers = this.timers.filter((t) => t !== h);
  };
  async advance(ms: number) {
    this.t += ms;
    for (const timer of this.timers.filter((x) => x.at <= this.t)) {
      this.timers = this.timers.filter((x) => x !== timer);
      timer.fn();
    }
    await flush();
  }
}

const D1 = "d1111111-1111-4111-8111-111111111111";
const D2 = "d2222222-2222-4222-8222-222222222222";

function signals(over: Partial<DocumentSignals> = {}): DocumentSignals {
  return { listChanged: false, documents: new Map(), active: null, reset: false, ...over };
}

function setup(version = 1) {
  const clock = new Clock();
  let current = full(D1, "One", { content_version: version });
  let etagNo = 1;
  const { api, calls } = fakeApi({
    [`GET /rooms/${ROOM}/documents?`]: () => ({ body: { items: [summary(D1, "One"), summary(D2, "Two")], next_cursor: null } }),
    [`GET /rooms/${ROOM}/documents/${D1}`]: (call) => {
      const etag = `"c${current.content_version}m1-${etagNo}"`;
      if (call.headers["If-None-Match"] === etag) return { status: 304, headers: { ETag: etag } };
      return { body: current, headers: { ETag: etag } };
    },
  });
  const store = new DocumentStore(api, ROOM, clock);
  return {
    store,
    calls,
    clock,
    bump: (v: number) => {
      current = { ...current, content_version: v };
    },
    fetches: () => calls.filter((c) => c.path.includes(`/documents/${D1}`)),
  };
}

describe("DocumentStore", () => {
  it("loads the list without content and pages with a cursor", async () => {
    const { store, calls } = setup();
    await store.loadList();
    expect(store.state.list.items.map((d) => d.title)).toEqual(["One", "Two"]);
    expect(calls[0]!.path).toContain("limit=20");
    expect(store.state.list.stale).toBe(false);
  });

  it("opens a document and keeps its ETag", async () => {
    const { store } = setup();
    await store.open(D1);
    expect(store.state.open?.doc?.title).toBe("One");
    expect(store.state.open?.etag).toBe('"c1m1-1"');
  });

  it("refetches with If-None-Match and a 304 changes nothing", async () => {
    const { store, fetches, clock } = setup();
    await store.open(D1);
    const before = store.state.open;
    await clock.advance(REFETCH_MIN_INTERVAL_MS + 1);
    store.refreshOpen();
    await flush();
    expect(fetches()).toHaveLength(2);
    expect(fetches()[1]!.headers["If-None-Match"]).toBe('"c1m1-1"');
    expect(store.state.open?.doc).toBe(before?.doc);
  });

  it("refetches at most once per second per document, with one trailing refetch", async () => {
    const { store, fetches, clock, bump } = setup();
    await store.open(D1);
    await clock.advance(REFETCH_MIN_INTERVAL_MS + 1);
    bump(2);
    for (let i = 0; i < 10; i++) store.refreshOpen(); // a burst
    await flush();
    expect(fetches()).toHaveLength(2); // one immediate refetch
    expect(clock.timers).toHaveLength(1); // ...and ONE trailing one scheduled
    await clock.advance(REFETCH_MIN_INTERVAL_MS);
    expect(fetches()).toHaveLength(3);
    expect(store.state.open?.doc?.content_version).toBe(2);
  });

  it("a content event for a document that is not open only marks the list stale and reloads it", async () => {
    const { store, calls } = setup();
    await store.open(D1);
    const docFetches = () => calls.filter((c) => c.path.includes(`/documents/${D1}`)).length;
    store.onSignals(signals({ listChanged: true, documents: new Map([[D2, { created: false, renamed: false, contentVersion: 5 }]]) }));
    await flush();
    expect(docFetches()).toBe(1); // the open document was NOT refetched
    expect(calls.some((c) => c.path.includes("/documents?limit"))).toBe(true);
  });

  it("ignores a content event at or below the version it already holds (its own saves)", async () => {
    const { store, fetches, clock } = setup(3);
    await store.open(D1);
    await clock.advance(REFETCH_MIN_INTERVAL_MS + 1);
    store.onSignals(signals({ listChanged: true, documents: new Map([[D1, { created: false, renamed: false, contentVersion: 3 }]]) }));
    store.onSignals(signals({ listChanged: true, documents: new Map([[D1, { created: false, renamed: false, contentVersion: 2 }]]) }));
    await flush();
    expect(fetches()).toHaveLength(1);
  });

  it("refetches the open document for a newer content version or a rename", async () => {
    const { store, fetches, clock, bump } = setup(1);
    await store.open(D1);
    await clock.advance(REFETCH_MIN_INTERVAL_MS + 1);
    bump(2);
    store.onSignals(signals({ listChanged: true, documents: new Map([[D1, { created: false, renamed: false, contentVersion: 2 }]]) }));
    await flush();
    expect(fetches()).toHaveLength(2);
    expect(store.state.open?.doc?.content_version).toBe(2);
    await clock.advance(REFETCH_MIN_INTERVAL_MS + 1);
    store.onSignals(signals({ listChanged: true, documents: new Map([[D1, { created: false, renamed: true, contentVersion: null }]]) }));
    await flush();
    expect(fetches()).toHaveLength(3);
  });

  it("reloads everything after a reset (a fresh bootstrap)", async () => {
    const { store, calls, clock } = setup();
    await store.open(D1);
    await clock.advance(REFETCH_MIN_INTERVAL_MS + 1);
    store.onSignals(signals({ reset: true }));
    await flush();
    expect(calls.some((c) => c.path.includes("/documents?limit"))).toBe(true);
    expect(calls.filter((c) => c.path.includes(`/documents/${D1}`))).toHaveLength(2);
  });

  it("a late answer for a document that is no longer open is discarded", async () => {
    const resolvers: (() => void)[] = [];
    const { api } = fakeApi({
      [`GET /rooms/${ROOM}/documents/${D1}`]: () => new Promise((res) => resolvers.push(() => res({ body: full(D1, "slow one") }))),
      [`GET /rooms/${ROOM}/documents/${D2}`]: () => ({ body: full(D2, "fast two") }),
    });
    const store = new DocumentStore(api, ROOM, new Clock());
    const slow = store.open(D1);
    await store.open(D2);
    resolvers[0]!();
    await slow;
    expect(store.state.open?.id).toBe(D2);
    expect(store.state.open?.doc?.title).toBe("fast two");
  });

  it("reports a missing document and a server error separately", async () => {
    const { api } = fakeApi({
      [`GET /rooms/${ROOM}/documents/${D1}`]: () => ({ status: 404, body: { error: { code: "document_not_found", message: "Document not found." } } }),
      [`GET /rooms/${ROOM}/documents/${D2}`]: () => ({ status: 500, body: { error: { code: "internal_error", message: "Something went wrong." } } }),
    });
    const store = new DocumentStore(api, ROOM, new Clock());
    await store.open(D1);
    expect(store.state.open).toMatchObject({ notFound: true, error: null });
    await store.open(D2);
    expect(store.state.open).toMatchObject({ notFound: false, error: "Something went wrong." });
  });

  it("notifies subscribers with a new snapshot object each change", async () => {
    const { store } = setup();
    const seen: unknown[] = [];
    store.subscribe(() => seen.push(store.getSnapshot()));
    await store.loadList();
    expect(seen.length).toBeGreaterThanOrEqual(2);
    expect(new Set(seen).size).toBe(seen.length);
  });
});
