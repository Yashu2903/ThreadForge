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
export const AI_STUB_PORT = 8190;
export const AI_STUB_URL = `http://127.0.0.1:${AI_STUB_PORT}`;
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
  LOG_LEVEL: "WARNING",
  STORAGE_PATH: `${BACKEND_DIR}data/e2e-storage`,
  INTERNAL_API_SECRET: INTERNAL_SECRET,
  COLLAB_SERVICE_URL: COLLAB_URL,
  // AI: the REAL adapters, worker and API against a local stand-in for the two providers (the same
  // stub the pytest suite uses). Server keys only, so no key-encryption setting is needed. These keys
  // are the stub's, not real credentials.
  AI_ENABLED: "true",
  AI_KEY_POLICY: "server_only",
  AI_OPENAI_API_KEY: "sk-server-openai-0123456789abcdef",
  AI_GOOGLE_API_KEY: "AIzaServerGoogleKey0123456789abcdef",
  AI_OPENAI_BASE_URL: AI_STUB_URL,
  AI_GOOGLE_BASE_URL: AI_STUB_URL,
  AI_LLM_MODEL: "test-llm",
  AI_LLM_CONTEXT_TOKENS: "100000",
  AI_LLM_MAX_OUTPUT_TOKENS: "4000",
  AI_EMBED_MODEL: "test-embed",
  AI_EMBED_DIMENSIONS: "8",
  AI_EMBED_MAX_INPUT_TOKENS: "8000",
  AI_MAX_INPUT_TOKENS: "50000",
  AI_MAX_OUTPUT_TOKENS: "2000",
  AI_RETRY_BASE_DELAY_SECONDS: "0.05",
  AI_RETRY_MAX_DELAY_SECONDS: "0.2",
  AI_WORKER_POLL_SECONDS: "0.5",
  AI_SELFCHECK_INTERVAL_SECONDS: "3600",
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
