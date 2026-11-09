import { defineConfig, devices } from "@playwright/test";
import { API_PORT, API_URL, BACKEND_DIR, COLLAB_URL, WEB_ORIGIN, WEB_PORT, backendEnv, collabEnv } from "./e2e/env";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false, // one database, one dispatcher: tests share state by design, so run serially
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  use: { baseURL: WEB_ORIGIN, trace: "retain-on-failure" },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    // a second engine for the editor: input handling differs between Blink and Gecko
    { name: "firefox", use: { ...devices["Desktop Firefox"] }, testMatch: /editor-.*\.spec\.ts/ },
  ],
  webServer: [
    {
      command: `uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port ${API_PORT} --no-access-log --ws-max-size 1024`,
      cwd: BACKEND_DIR,
      env: backendEnv,
      url: `${API_URL}/health/ready`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `npx vite --port ${WEB_PORT} --strictPort`,
      env: { ...(process.env as Record<string, string>), THREADFORGE_API: API_URL, THREADFORGE_COLLAB: COLLAB_URL },
      url: WEB_ORIGIN,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
