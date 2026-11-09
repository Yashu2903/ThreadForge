/**
 * The HTTP client. One place that knows about: cookies (sent with credentials), the CSRF token on
 * unsafe methods, and the backend's error envelope {"error": {"code", "message", "request_id", ...}}.
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly extra: Record<string, unknown> = {},
    readonly retryAfter: number | null = null,
  ) {
    super(message);
  }
}

export interface ApiResponse<T> {
  status: number;
  body: T;
  headers: Headers;
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export class Api {
  private csrf: string | null = null;

  constructor(
    private readonly base = "",
    private readonly doFetch: FetchLike = (input, init) => fetch(input, init),
  ) {}

  get httpBase(): string {
    return this.base || window.location.origin;
  }

  async get<T>(path: string, headers: Record<string, string> = {}): Promise<ApiResponse<T>> {
    return this.request<T>("GET", path, undefined, headers);
  }

  async send<T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, json?: unknown): Promise<ApiResponse<T>> {
    return this.request<T>(method, path, json, {});
  }

  /** Send raw bytes (an image upload) with the CSRF token; returns the parsed JSON answer. */
  async upload<T>(path: string, body: Blob, contentType: string, method: "POST" | "PUT" = "POST"): Promise<ApiResponse<T>> {
    for (const force of [false, true]) {
      const res = await this.doFetch(`${this.base}${path}`, {
        method,
        headers: { "X-CSRF-Token": await this.token(force), "Content-Type": contentType },
        credentials: "include",
        body,
      });
      if (res.ok) return { status: res.status, body: (await res.json()) as T, headers: res.headers };
      const error = await toError(res);
      if (error.code !== "csrf_failed" || force) throw error;
    }
    throw new ApiError(0, "unreachable", "unreachable");
  }

  private async token(force = false): Promise<string> {
    if (this.csrf && !force) return this.csrf;
    const res = await this.doFetch(`${this.base}/auth/csrf`, { credentials: "include" });
    if (!res.ok) throw await toError(res);
    this.csrf = ((await res.json()) as { csrf_token: string }).csrf_token;
    return this.csrf;
  }

  private async request<T>(
    method: string,
    path: string,
    json: unknown,
    extra: Record<string, string>,
    retried = false,
  ): Promise<ApiResponse<T>> {
    const headers: Record<string, string> = { ...extra };
    const unsafe = method !== "GET" && method !== "HEAD";
    if (unsafe) headers["X-CSRF-Token"] = await this.token(retried);
    if (json !== undefined) headers["Content-Type"] = "application/json";
    const res = await this.doFetch(`${this.base}${path}`, {
      method,
      headers,
      credentials: "include",
      body: json === undefined ? undefined : JSON.stringify(json),
    });
    if (res.status === 304) return { status: 304, body: undefined as T, headers: res.headers };
    if (!res.ok) {
      const error = await toError(res);
      // a stale token (e.g. the session was rotated): fetch a new one once and repeat
      if (unsafe && !retried && error.code === "csrf_failed") return this.request(method, path, json, extra, true);
      throw error;
    }
    const body = res.status === 204 ? (undefined as T) : ((await res.json()) as T);
    return { status: res.status, body, headers: res.headers };
  }
}

async function toError(res: Response): Promise<ApiError> {
  let code = "http_error";
  let message = `Request failed (${res.status}).`;
  let extra: Record<string, unknown> = {};
  try {
    const { error } = (await res.json()) as { error: { code: string; message: string } & Record<string, unknown> };
    const { code: c, message: m, request_id: _requestId, ...rest } = error;
    code = c;
    message = m;
    extra = rest;
  } catch {
    // not an envelope (a proxy error page, for example): keep the generic text
  }
  const retry = res.headers.get("Retry-After");
  return new ApiError(res.status, code, message, extra, retry ? Number(retry) : null);
}
