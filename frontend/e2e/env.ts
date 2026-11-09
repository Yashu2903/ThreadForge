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
export const COLLAB_PORT = 4010;
export const COLLAB_URL = `http://127.0.0.1:${COLLAB_PORT}`;
export const INTERNAL_SECRET = "e2e-internal-secret-0123456789-abcdefghijklmnop";
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
  STORAGE_PATH: `${BACKEND_DIR}data/e2e-storage`,
  INTERNAL_API_SECRET: INTERNAL_SECRET,
};

/** The collaboration service (Node, Hocuspocus): talks to the API's /internal/collab routes only. */
export const collabEnv: Record<string, string> = {
  ...(process.env as Record<string, string>),
  COLLAB_ADDRESS: "127.0.0.1",
  COLLAB_PORT: String(COLLAB_PORT),
  COLLAB_API_BASE_URL: API_URL,
  INTERNAL_API_SECRET: INTERNAL_SECRET,
  COLLAB_ALLOWED_ORIGINS: WEB_ORIGIN,
  COLLAB_LOG_LEVEL: "warn",
  // room hints (the dispatcher publishes them) make access changes take effect in about a second
  COLLAB_REDIS_URL: "redis://127.0.0.1:6390/14",
  COLLAB_REDIS_PREFIX: prefix,
  COLLAB_ACCESS_CHECK_MS: "3000",
};
