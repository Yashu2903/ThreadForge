/**
 * One environment for the collaboration tests: the REAL FastAPI app (uvicorn) on its own port against
 * a dedicated PostgreSQL database, and the real collaboration server started by each test file.
 */
export const API_PORT = 8110;
export const API_URL = `http://127.0.0.1:${API_PORT}`;
export const ORIGIN = "http://localhost:5173";
export const INTERNAL_SECRET = "collab-test-internal-secret-0123456789-abcdef";
export const DB_NAME = "threadforge_collab_e2e";
export const DATABASE_URL_ASYNC = `postgresql+asyncpg://threadforge:threadforge-dev-password@127.0.0.1:5442/${DB_NAME}`;
export const DATABASE_URL_PG = `postgresql://threadforge:threadforge-dev-password@127.0.0.1:5442/${DB_NAME}`;
export const BACKEND_DIR = new URL("../../backend/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

export const backendEnv: Record<string, string> = {
  ...(process.env as Record<string, string>),
  APP_ENV: "development",
  DATABASE_URL: DATABASE_URL_ASYNC,
  GOOGLE_CLIENT_ID: "collab.apps.googleusercontent.com",
  GOOGLE_CLIENT_SECRET: "collab-not-used-by-these-tests",
  PUBLIC_BASE_URL: ORIGIN,
  OIDC_CALLBACK_URL: `${ORIGIN}/auth/callback`,
  TRUSTED_ORIGINS: ORIGIN,
  CSRF_SECRET: "collab-csrf-secret-0123456789-abcdefghijklmnop",
  MESSAGE_FINGERPRINT_SECRET: "collab-fingerprint-secret-0123456789-abcdefgh",
  REDIS_URL: "redis://127.0.0.1:6390/14",
  REDIS_CHANNEL_PREFIX: "collab-tests",
  INTERNAL_API_SECRET: INTERNAL_SECRET,
  LOG_LEVEL: "WARNING",
  STORAGE_PATH: `${BACKEND_DIR}data/collab-test-storage`,
};
