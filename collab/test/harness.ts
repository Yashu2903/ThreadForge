/** Helpers: start the real collaboration server, connect real Hocuspocus providers to it. */
import { HocuspocusProvider } from "@hocuspocus/provider";
import { YJS_FIELD, extractText, type PMJson } from "@threadforge/editor-schema";
import { yDocToProsemirrorJSON } from "@tiptap/y-tiptap";
import WebSocket from "ws";
import * as Y from "yjs";
import { ApiError, InternalApi } from "../src/api";
import { DEFAULTS, type CollabConfig } from "../src/config";
import { CollabServer, type CollabDeps } from "../src/server";
import { API_URL, INTERNAL_SECRET, ORIGIN } from "./env";
import { pool, type TestUser } from "./fixtures";

export function testConfig(overrides: Partial<CollabConfig> = {}): CollabConfig {
  return {
    ...DEFAULTS,
    port: 0,
    apiBaseUrl: API_URL,
    internalSecret: INTERNAL_SECRET,
    allowedOrigins: [ORIGIN],
    logLevel: "silent",
    debounceMs: 80,
    maxDebounceMs: 400,
    snapshotIntervalMs: 250,
    leaseRenewMs: 500,
    leaseGraceMs: 3_000,
    storeRetryDelayMs: 20,
    storeRecoveryDelayMs: 200,
    ...overrides,
  };
}

const running: CollabServer[] = [];

export async function startCollab(overrides: Partial<CollabConfig> = {}, deps: CollabDeps = {}): Promise<CollabServer> {
  const server = new CollabServer(testConfig(overrides), { onLeaseLost: () => undefined, ...deps });
  await server.start();
  running.push(server);
  return server;
}

export async function stopAll(): Promise<void> {
  for (const providerEntry of providers.splice(0)) providerEntry.destroy();
  for (const server of running.splice(0)) await server.stop();
  // a test may leave the lease with another holder: start the next test from a free lease
  await pool.query("UPDATE collab_lease SET holder_id = NULL, expires_at = now()");
}

const providers: HocuspocusProvider[] = [];

export interface Client {
  provider: HocuspocusProvider;
  ydoc: Y.Doc;
  /** resolves when synced, rejects with the server's reason if authentication fails */
  ready: Promise<void>;
  closes: { code: number; reason: string }[];
  denied: string[];
}

export function connect(
  server: CollabServer,
  user: Pick<TestUser, "cookie"> | null,
  docId: string,
  opts: { origin?: string | null; name?: string; path?: string } = {},
): Client {
  const origin = opts.origin === undefined ? ORIGIN : opts.origin;
  class AuthedSocket extends WebSocket {
    constructor(url: string, protocols?: string | string[]) {
      const headers: Record<string, string> = {};
      if (user) headers.Cookie = user.cookie;
      if (origin) headers.Origin = origin;
      super(url, protocols, { headers });
    }
  }
  const ydoc = new Y.Doc();
  const closes: Client["closes"] = [];
  const denied: string[] = [];
  let resolve!: () => void;
  let reject!: (e: Error) => void;
  const ready = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  ready.catch(() => undefined); // tests that expect rejection await it themselves
  // the websocket options (url, WebSocketPolyfill, maxAttempts) are accepted at runtime but not in the type
  const config: any = {
    url: `ws://127.0.0.1:${server.port}${opts.path ?? "/collab"}`,
    name: opts.name ?? `doc:${docId}`,
    document: ydoc,
    token: "unused",
    WebSocketPolyfill: AuthedSocket as never,
    maxAttempts: 1,
    onSynced: () => resolve(),
    onAuthenticationFailed: ({ reason }: { reason: string }) => {
      denied.push(reason);
      reject(new Error(`denied:${reason}`));
    },
    onClose: ({ event }: { event: { code: number; reason: string } }) => closes.push({ code: event.code, reason: event.reason }),
  };
  const provider = new HocuspocusProvider(config);
  providers.push(provider);
  return { provider, ydoc, ready, closes, denied };
}

export async function until<T>(fn: () => T | Promise<T>, what = "condition", timeoutMs = 8_000): Promise<NonNullable<T>> {
  const deadline = Date.now() + timeoutMs;
  let last: unknown;
  for (;;) {
    try {
      const value = await fn();
      if (value) return value as NonNullable<T>;
    } catch (error) {
      last = error;
    }
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}${last ? ` (${String(last)})` : ""}`);
    await new Promise((r) => setTimeout(r, 40));
  }
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function docJson(ydoc: Y.Doc): PMJson {
  return yDocToProsemirrorJSON(ydoc, YJS_FIELD) as PMJson;
}
export function docText(ydoc: Y.Doc): string {
  return extractText(docJson(ydoc));
}

/** Type into the first paragraph the way the editor would at the Yjs level. */
export function appendText(ydoc: Y.Doc, text: string): void {
  const fragment = ydoc.getXmlFragment(YJS_FIELD);
  const paragraph = fragment.get(0) as Y.XmlElement;
  const last = paragraph.length > 0 ? paragraph.get(paragraph.length - 1) : null;
  if (last instanceof Y.XmlText) last.insert(last.length, text);
  else {
    const t = new Y.XmlText();
    paragraph.insert(paragraph.length, [t]);
    t.insert(0, text);
  }
}

/** The real API client with switchable faults, to test retries and outages. */
export class FaultyApi extends InternalApi {
  failPuts = 0;
  failGetState = 0;
  constructor() {
    super(API_URL, INTERNAL_SECRET, 5_000);
  }
  override async putState(...args: Parameters<InternalApi["putState"]>): Promise<void> {
    if (this.failPuts > 0) {
      this.failPuts -= 1;
      throw new ApiError(0, "unreachable");
    }
    return super.putState(...args);
  }
  override async getState(...args: Parameters<InternalApi["getState"]>) {
    if (this.failGetState > 0) {
      this.failGetState -= 1;
      throw new ApiError(0, "unreachable");
    }
    return super.getState(...args);
  }
}

export const internalApi = () => new InternalApi(API_URL, INTERNAL_SECRET, 5_000);
