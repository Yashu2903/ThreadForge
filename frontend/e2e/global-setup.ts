import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { BACKEND_DIR, COLLAB_URL, collabEnv } from "./env";
import { backendEnv } from "./env";

let dispatcher: ChildProcess | null = null;
let collab: ChildProcess | null = null;
const COLLAB_DIR = new URL("../../collab/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function killTree(child: ChildProcess | null): void {
  if (!child || !child.pid) return;
  if (process.platform === "win32") {
    try {
      execFileSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], { stdio: "ignore" });
    } catch {
      /* already gone */
    }
  } else {
    child.kill("SIGTERM");
  }
}

async function waitFor(url: string, ms: number): Promise<void> {
  const end = Date.now() + ms;
  for (;;) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    if (Date.now() > end) throw new Error(`${url} did not become healthy`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

/** Create/migrate/empty threadforge_e2e, then run the outbox dispatcher (it publishes the Redis hints). */
export default async function globalSetup(): Promise<() => Promise<void>> {
  execFileSync("uv", ["run", "python", "-m", "scripts.e2e_prepare"], { cwd: BACKEND_DIR, env: backendEnv, stdio: "inherit" });
  dispatcher = spawn("uv", ["run", "python", "-m", "app.workers.outbox_dispatcher"], {
    cwd: BACKEND_DIR,
    env: backendEnv,
    stdio: "ignore",
    windowsHide: true,
  });
  // The collaboration service starts AFTER the database was prepared: it takes the single-instance
  // lease in the table that e2e_prepare just emptied.
  if (!existsSync(`${COLLAB_DIR}dist/main.js`)) throw new Error("Run `npm run build` at the repository root first (collab/dist is missing).");
  collab = spawn("node", ["dist/main.js"], { cwd: COLLAB_DIR, env: collabEnv, stdio: "inherit", windowsHide: true });
  await waitFor(`${COLLAB_URL}/health`, 30_000);
  return async () => {
    killTree(collab);
    killTree(dispatcher);
  };
}
