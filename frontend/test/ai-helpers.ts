/** A fake backend for the AI UI tests: a real AiStore talking to an Api whose fetch is a routing table. */
import { vi } from "vitest";
import { Api } from "../src/api";
import { AiStore } from "../src/ai/store";
import type { AiSnapshot } from "../src/ai/store";
import type { AiResult, RequestInfo, SourceItem } from "../src/ai/types";

export type Handler = (body: unknown, url: string) => { status?: number; json?: unknown };

export class FakeServer {
  calls: { method: string; path: string; body: unknown }[] = [];
  routes = new Map<string, Handler>();

  on(method: string, path: string | RegExp, handler: Handler | unknown): this {
    const spec = handler as { status?: number; json?: unknown } | null;
    const isSpec = spec !== null && typeof spec === "object" && "json" in spec && "status" in spec; // {status, json}
    const h: Handler = typeof handler === "function" ? (handler as Handler) : isSpec ? () => spec! : () => ({ json: handler });
    this.routes.set(`${method} ${path instanceof RegExp ? path.source : path}`, Object.assign(h, { regex: path instanceof RegExp ? path : null }));
    return this;
  }

  fetch = vi.fn(async (input: string, init?: RequestInit): Promise<Response> => {
    const method = (init?.method ?? "GET").toUpperCase();
    const url = String(input);
    const path = url.split("?")[0]!;
    if (path === "/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }), { status: 200 });
    let body: unknown = init?.body ?? undefined;
    if (typeof body === "string") {
      try {
        body = JSON.parse(body);
      } catch {
        /* raw */
      }
    }
    this.calls.push({ method, path: url, body });
    for (const [key, handler] of this.routes) {
      const [m, ...rest] = key.split(" ");
      if (m !== method) continue;
      const pattern = rest.join(" ");
      const regex = (handler as unknown as { regex: RegExp | null }).regex;
      if (regex ? regex.test(path) : pattern === path) {
        const out = handler(body, url);
        return new Response(out.status === 204 ? null : JSON.stringify(out.json ?? {}), { status: out.status ?? 200, headers: { "content-type": "application/json" } });
      }
    }
    return new Response(JSON.stringify({ error: { code: "not_found", message: "no route" } }), { status: 404 });
  });

  api(): Api {
    return new Api("", this.fetch as unknown as (input: string, init?: RequestInit) => Promise<Response>);
  }

  sent(method: string, pathPart: string) {
    return this.calls.filter((c) => c.method === method && c.path.includes(pathPart));
  }
}

export const ROOM = "11111111-1111-4111-8111-111111111111";
export const ME = "22222222-2222-4222-8222-222222222222";
export const DOC = "33333333-3333-4333-8333-333333333333";

export const STATUS_OK = {
  available: true,
  code: "ok",
  configured: true,
  globally_enabled: true,
  key_policy: "user_if_present",
  notice_version: 1,
  capabilities: {},
  spend_controls: { prices_configured: true, budgets_enforced: false },
};
export const CONSENT_OK = {
  notice_version: 1,
  accepted: true,
  points: ["Your files are sent to the embedding provider."],
  key_sources: {
    generation: { provider: "openai", source: "server", problem: null },
    embedding: { provider: "google", source: "server", problem: null },
  },
};

export function source(id: string, name: string, state: SourceItem["current_version"] extends infer V ? string : string = "ready", extra: Record<string, unknown> = {}): SourceItem {
  return {
    id: `s-${id}`,
    display_name: name,
    created_by: ME,
    version_count: 1,
    current_version: {
      id: `v-${id}`,
      source_id: `s-${id}`,
      version_no: 1,
      original_name: name,
      kind: "pdf",
      byte_size: 20480,
      analysis_state: state as never,
      page_count: 12,
      likely_scanned_pages: 0,
      image_count: 0,
      text_chars: 1000,
      error_code: null,
      ...extra,
    },
  };
}

export function serverWith(opts: { role?: "owner" | "editor" | "viewer"; sources?: SourceItem[]; consentAccepted?: boolean; status?: Record<string, unknown>; aiEnabled?: boolean } = {}) {
  const server = new FakeServer();
  server
    .on("GET", "/ai/status", { ...STATUS_OK, ...(opts.status ?? {}) })
    .on("GET", "/ai/consent", { ...CONSENT_OK, accepted: opts.consentAccepted ?? true })
    .on("GET", "/ai/keys", { items: [] })
    .on("GET", `/rooms/${ROOM}/ai-settings`, { ai_enabled: opts.aiEnabled ?? true, available: true, code: "ok" })
    .on("GET", `/rooms/${ROOM}/sources`, {
      sources: opts.sources ?? [],
      limits: { max_upload_bytes: 26214400, max_sources: 100, room_quota_bytes: 1, accepted: ["pdf"] },
      used_sources: 0,
      used_bytes: 0,
      can_upload: opts.role !== "viewer",
      can_download: true,
    });
  return server;
}

export async function loadedStore(server: FakeServer): Promise<{ store: AiStore; snap: AiSnapshot }> {
  const store = new AiStore(server.api(), ROOM, ME);
  await store.load();
  return { store, snap: store.getSnapshot() };
}

export function requestInfo(over: Partial<RequestInfo> = {}): RequestInfo {
  return {
    id: "r1",
    message_id: "m1",
    requested_by: ME,
    state: "queued",
    stage_label: null,
    task_type: null,
    error_code: null,
    error_detail: null,
    attempt: 1,
    max_attempts: 3,
    sources: [],
    queue_position: 1,
    can_cancel: true,
    can_retry: false,
    reply_message_id: null,
    ...over,
  };
}

export function resultOf(over: Partial<AiResult> = {}): AiResult {
  return {
    request_id: "r1",
    reply_message_id: "m2",
    task_type: "qa",
    redacted: false,
    answer: {
      blocks: [
        { type: "paragraph", text: "The warehouse processed 12,000 orders.", citations: [1], unsupported: false },
        { type: "paragraph", text: "Nothing backs this claim.", citations: [], unsupported: true },
        { type: "bullet_list", items: [{ text: "Drivers were short", citations: [2], unsupported: false }] },
      ],
      evidence_gaps: ["No 2024 data"],
    },
    citations: [
      { n: 1, label: "P1", kind: "passage", source_version_id: "v-a", source_name: "ops.pdf", locator: { page_start: 3 }, extraction: "text_layer", machine_generated: false, low_agreement: false, figure_chunk_id: null, excerpt: "twelve thousand orders in March", quotes: ["twelve thousand orders"] },
      { n: 2, label: "P2", kind: "passage", source_version_id: "v-a", source_name: "ops.pdf", locator: { page_start: 5 }, extraction: "tesseract", machine_generated: true, low_agreement: false, figure_chunk_id: null, excerpt: "scanned text" },
    ],
    coverage: { sources: [{ source_version_id: "v-a", source_name: "ops.pdf", mode: "retrieval", sections_total: 8, sections_read: 3, figures_total: 4, figures_examined: 1 }], document: { mode: "full", blocks_total: 5, blocks_included: 5, captured_update_counter: 7, scope: "whole" } },
    models: {},
    citation_text_visible: true,
    ...over,
  };
}
