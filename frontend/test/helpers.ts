import { Api, type FetchLike } from "../src/api";
import type { DocumentFull, DocumentSummary } from "../src/types";

export const ROOM = "11111111-1111-4111-8111-111111111111";

export interface Call {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}
export type Responder = (call: Call) => { status?: number; body?: unknown; headers?: Record<string, string> } | Promise<{ status?: number; body?: unknown; headers?: Record<string, string> }>;

/** An Api whose fetch is scripted: routes are matched by "METHOD path-prefix", longest first. */
export function fakeApi(routes: Record<string, Responder>) {
  const calls: Call[] = [];
  const doFetch: FetchLike = async (input, init) => {
    const method = init?.method ?? "GET";
    const call: Call = {
      method,
      path: input,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? JSON.parse(init.body as string) : undefined,
    };
    calls.push(call);
    const key = Object.keys(routes)
      .filter((k) => `${method} ${input}`.startsWith(k))
      .sort((a, b) => b.length - a.length)[0];
    if (!key) throw new Error(`no fake route for ${method} ${input}`);
    const out = await routes[key]!(call);
    const status = out.status ?? 200;
    const body = status === 304 || status === 204 ? null : JSON.stringify(out.body ?? {});
    return new Response(body, { status, headers: { "Content-Type": "application/json", ...(out.headers ?? {}) } });
  };
  return { api: new Api("", doFetch), calls };
}

export const csrfRoute: Record<string, Responder> = { "GET /auth/csrf": () => ({ body: { csrf_token: "tok" } }) };

export function summary(id: string, title = "Doc", extra: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id,
    room_id: ROOM,
    title,
    template_key: "blank",
    schema_version: 1,
    metadata_version: 1,
    created_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    collab_state: "legacy",
    is_active: false,
    requires_review: false,
    ...extra,
  };
}

export function full(id: string, title = "Doc", extra: Partial<DocumentFull> = {}): DocumentFull {
  return {
    ...summary(id, title),
    can_edit: true,
    collab_path: "/collab",
    document_name: `doc:${id}`,
    ...extra,
  };
}

export const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
