import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const client = (file: string) => fileURLToPath(new URL(`../client/src/${file}`, import.meta.url));
const backend = process.env.THREADFORGE_API ?? "http://127.0.0.1:8000";
// the collaboration (Yjs/Hocuspocus) service; the browser reaches it at /collab on this same origin
const collab = process.env.THREADFORGE_COLLAB ?? "http://127.0.0.1:4010";

// The browser talks to ONE origin (this dev server); it forwards the API paths to the backend, so
// the session cookie is first-party and there is no CORS. The SPA itself uses #/hash routes, so no
// page path can collide with an API prefix. Add this origin (http://localhost:5173) to the
// backend's TRUSTED_ORIGINS: the CSRF and WebSocket Origin checks compare it exactly.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      { find: "@threadforge/editor-schema", replacement: fileURLToPath(new URL("../packages/editor-schema/src/index.ts", import.meta.url)) },
      { find: "threadforge-client/document-schema", replacement: client("document-schema.ts") },
      { find: "threadforge-client", replacement: client("index.ts") },
    ],
  },
  server: {
    port: 5173,
    proxy: {
      "/auth": { target: backend, changeOrigin: false },
      "/capabilities": { target: backend, changeOrigin: false },
      "/health": { target: backend, changeOrigin: false },
      "/rooms": { target: backend, changeOrigin: false, ws: true },
      // WebSocket only. The Origin header is forwarded unchanged: the service compares it exactly.
      "/collab": { target: collab, changeOrigin: false, ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.{ts,tsx}"],
  },
});
