import { defineConfig } from "vitest/config";

// The collaboration tests start a REAL FastAPI server against a REAL PostgreSQL database (see
// test/global-setup.ts) and real WebSocket clients, so they are slower than unit tests and run
// in one worker, files one after another.
export default defineConfig({
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
    globalSetup: ["test/global-setup.ts"],
    testTimeout: 30_000,
    hookTimeout: 120_000,
    fileParallelism: false,
  },
});
