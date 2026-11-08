import { randomBytes } from "node:crypto";

/**
 * One environment for everything the browser tests start: the API, the outbox dispatcher and the
 * seeding script. A dedicated database (threadforge_e2e), a dedicated Redis database index (14,
 * the pytest suite uses 15) and a per-run channel prefix, so a run can never touch development data.
 */
export const API_PORT = 8100;
export const WEB_PORT = 5173;
export const WEB_ORIGIN = `http://localhost:${WEB_PORT}`;
export const API_URL = `http://127.0.0.1:${API_PORT}`;
export const BACKEND_DIR = new URL("../../backend/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

const prefix = process.env.E2E_PREFIX ?? `e2e-${randomBytes(4).toString("hex")}`;
process.env.E2E_PREFIX = prefix;

export const backendEnv: Record<string, string> = {
  ...(process.env as Record<string, string>),
  APP_ENV: "development",
  DATABASE_URL: "postgresql+asyncpg://threadforge:threadforge-dev-password@127.0.0.1:5442/threadforge_e2e",
  GOOGLE_CLIENT_ID: "e2e.apps.googleusercontent.com",
  GOOGLE_CLIENT_SECRET: "e2e-not-used-by-these-tests",
  PUBLIC_BASE_URL: WEB_ORIGIN,
  OIDC_CALLBACK_URL: `${WEB_ORIGIN}/auth/callback`,
  TRUSTED_ORIGINS: WEB_ORIGIN,
  CSRF_SECRET: "e2e-csrf-secret-0123456789-abcdefghijklmnop",
  MESSAGE_FINGERPRINT_SECRET: "e2e-fingerprint-secret-0123456789-abcdefghij",
  REDIS_URL: "redis://127.0.0.1:6390/14",
  REDIS_CHANNEL_PREFIX: prefix,
  OUTBOX_POLL_INTERVAL_MS: "100",
  MESSAGE_WRITES_PER_MINUTE: "1000",
  DOCUMENT_WRITES_PER_MINUTE: "1000",
  LOG_LEVEL: "WARNING",
};
