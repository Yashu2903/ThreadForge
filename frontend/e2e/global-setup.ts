import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { BACKEND_DIR, backendEnv } from "./env";

let dispatcher: ChildProcess | null = null;

/** Create/migrate/empty threadforge_e2e, then run the outbox dispatcher (it publishes the Redis hints). */
export default async function globalSetup(): Promise<() => Promise<void>> {
  execFileSync("uv", ["run", "python", "-m", "scripts.e2e_prepare"], { cwd: BACKEND_DIR, env: backendEnv, stdio: "inherit" });
  dispatcher = spawn("uv", ["run", "python", "-m", "app.workers.outbox_dispatcher"], {
    cwd: BACKEND_DIR,
    env: backendEnv,
    stdio: "ignore",
    windowsHide: true,
  });
  return async () => {
    if (dispatcher && dispatcher.pid) {
      if (process.platform === "win32") {
        // uv spawns the python child; kill the whole tree
        try {
          execFileSync("taskkill", ["/pid", String(dispatcher.pid), "/T", "/F"], { stdio: "ignore" });
        } catch {
          /* already gone */
        }
      } else {
        dispatcher.kill("SIGTERM");
      }
    }
  };
}
