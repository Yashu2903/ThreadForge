import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { API_URL, API_PORT, BACKEND_DIR, DATABASE_URL_ASYNC, backendEnv } from "./env";

let api: ChildProcess | null = null;

async function waitForApi(): Promise<void> {
  const deadline = Date.now() + 60_000;
  for (;;) {
    try {
      if ((await fetch(`${API_URL}/health/ready`)).ok) return;
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) throw new Error("the test API did not become ready");
    await new Promise((r) => setTimeout(r, 300));
  }
}

/** Create/migrate/empty the dedicated database, then run the real API against it. */
export default async function setup(): Promise<() => Promise<void>> {
  execFileSync("uv", ["run", "python", "-m", "scripts.e2e_prepare", DATABASE_URL_ASYNC], {
    cwd: BACKEND_DIR,
    env: backendEnv,
    stdio: "inherit",
  });
  api = spawn("uv", ["run", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", String(API_PORT), "--no-access-log"], {
    cwd: BACKEND_DIR,
    env: backendEnv,
    stdio: "ignore",
    windowsHide: true,
  });
  await waitForApi();
  return async () => {
    if (api?.pid) {
      if (process.platform === "win32") {
        try {
          execFileSync("taskkill", ["/pid", String(api.pid), "/T", "/F"], { stdio: "ignore" });
        } catch {
          /* already gone */
        }
      } else api.kill("SIGTERM");
    }
  };
}
