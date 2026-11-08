import { execFileSync } from "node:child_process";
import { type APIRequestContext, type Browser, type BrowserContext, type Page, request } from "@playwright/test";
import { API_URL, BACKEND_DIR, WEB_ORIGIN, backendEnv } from "./env";

export interface Seeded {
  user_id: string;
  cookie_name: string;
  secret: string;
  name: string;
}

/** Create a user + real session through backend/scripts/seed_session.py (no impersonation endpoint exists). */
export function seed(name: string, join?: { roomId: string; role: "editor" | "viewer" }): Seeded {
  const args = ["run", "python", "-m", "scripts.seed_session", "--name", name];
  if (join) args.push("--join", join.roomId, "--role", join.role);
  const out = execFileSync("uv", args, { cwd: BACKEND_DIR, env: backendEnv, encoding: "utf-8" });
  const line = out.trim().split("\n").pop()!;
  return { ...(JSON.parse(line) as Omit<Seeded, "name">), name };
}

/** A browser context that is signed in as `user` (the cookie the real login would have set). */
export async function signedIn(browser: Browser, user: Seeded): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ baseURL: WEB_ORIGIN });
  await context.addCookies([{ name: user.cookie_name, value: user.secret, url: WEB_ORIGIN }]);
  return { context, page: await context.newPage() };
}

/** API calls AS a user, through the same origin the browser uses (so CSRF/Origin rules apply for real). */
export class AsUser {
  private csrf: string | null = null;
  private constructor(private readonly ctx: APIRequestContext) {}
  static async create(user: Seeded): Promise<AsUser> {
    const ctx = await request.newContext({
      baseURL: API_URL,
      extraHTTPHeaders: { Cookie: `${user.cookie_name}=${user.secret}`, Origin: WEB_ORIGIN },
    });
    return new AsUser(ctx);
  }
  async token(): Promise<string> {
    if (!this.csrf) this.csrf = ((await (await this.ctx.get("/auth/csrf")).json()) as { csrf_token: string }).csrf_token;
    return this.csrf;
  }
  async get<T = any>(path: string): Promise<T> {
    const res = await this.ctx.get(path);
    if (!res.ok()) throw new Error(`GET ${path} -> ${res.status()}: ${await res.text()}`);
    return (await res.json()) as T;
  }
  async send<T = any>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, data: unknown): Promise<{ status: number; body: T }> {
    const res = await this.ctx.fetch(path, { method, data, headers: { "X-CSRF-Token": await this.token() } });
    return { status: res.status(), body: (await res.json().catch(() => null)) as T };
  }
  async createRoom(name: string): Promise<string> {
    const { status, body } = await this.send<{ id: string }>("POST", "/rooms", { name });
    if (status !== 201) throw new Error(`create room failed: ${status}`);
    return body.id;
  }
  async createDocument(roomId: string, title: string, template = "blank"): Promise<any> {
    const { status, body } = await this.send("POST", `/rooms/${roomId}/documents`, {
      title,
      template_key: template,
      client_request_id: crypto.randomUUID(),
    });
    if (status !== 201) throw new Error(`create document failed: ${status} ${JSON.stringify(body)}`);
    return body;
  }
  async dispose(): Promise<void> {
    await this.ctx.dispose();
  }
}

export const roomUrl = (roomId: string) => `/#/rooms/${roomId}`;
