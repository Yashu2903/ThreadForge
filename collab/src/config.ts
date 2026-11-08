/** Configuration, from the environment (main.ts) or passed directly (tests). */
import { DEFAULT_LIMITS, type Limits } from "@threadforge/editor-schema";
import type { Level } from "./log.js";

export interface CollabConfig {
  port: number;
  address: string;
  /** Path the WebSocket is served on. The reverse proxy forwards /collab without rewriting it. */
  path: string;
  /** Base URL of the FastAPI app on the PRIVATE network, e.g. http://api:8000 */
  apiBaseUrl: string;
  internalSecret: string;
  /** Exact-match list. A browser's Origin header must equal one of these. */
  allowedOrigins: string[];
  logLevel: Level;
  /** store debounce: 2 s after the last change, at most 10 s after the first unsaved one */
  debounceMs: number;
  maxDebounceMs: number;
  snapshotIntervalMs: number;
  leaseRenewMs: number;
  /** how long after a failed renew we keep writing before declaring the lease lost */
  leaseGraceMs: number;
  maxMessageBytes: number;
  maxStateBytes: number;
  maxConnectionsPerUser: number;
  maxConnectionsTotal: number;
  storeRetries: number;
  storeRetryDelayMs: number;
  /** after a store fails for good, try again this much later (the document stays in memory) */
  storeRecoveryDelayMs: number;
  apiTimeoutMs: number;
  limits: Limits;
}

export const DEFAULTS: Omit<CollabConfig, "apiBaseUrl" | "internalSecret" | "allowedOrigins"> = {
  port: 4010,
  address: "127.0.0.1",
  path: "/collab",
  logLevel: "info",
  debounceMs: 2_000,
  maxDebounceMs: 10_000,
  snapshotIntervalMs: 30_000,
  leaseRenewMs: 10_000,
  leaseGraceMs: 25_000,
  maxMessageBytes: 1024 * 1024,
  maxStateBytes: 8 * 1024 * 1024,
  maxConnectionsPerUser: 10,
  maxConnectionsTotal: 500,
  storeRetries: 3,
  storeRetryDelayMs: 500,
  storeRecoveryDelayMs: 5_000,
  apiTimeoutMs: 10_000,
  limits: DEFAULT_LIMITS,
};

function int(env: NodeJS.ProcessEnv, name: string, fallback: number): number {
  const raw = env[name];
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) throw new Error(`${name} must be a non-negative integer.`);
  return value;
}

export function configFromEnv(env: NodeJS.ProcessEnv = process.env): CollabConfig {
  const problems: string[] = [];
  const apiBaseUrl = (env.COLLAB_API_BASE_URL ?? "").replace(/\/+$/, "");
  const internalSecret = env.INTERNAL_API_SECRET ?? "";
  const origins = (env.COLLAB_ALLOWED_ORIGINS ?? env.TRUSTED_ORIGINS ?? "")
    .split(",")
    .map((o) => o.trim())
    .filter(Boolean);
  if (!/^https?:\/\/[^/]+/.test(apiBaseUrl)) problems.push("COLLAB_API_BASE_URL must be an absolute http(s) URL.");
  if (internalSecret.length < 16) problems.push("INTERNAL_API_SECRET must be set (at least 16 characters).");
  if (origins.length === 0) problems.push("COLLAB_ALLOWED_ORIGINS (or TRUSTED_ORIGINS) must list the allowed browser origins.");
  if (origins.some((o) => o === "*" || !/^https?:\/\/[^/]+$/.test(o))) {
    problems.push("COLLAB_ALLOWED_ORIGINS entries must be exact origins such as https://app.example.com (no wildcard, no path).");
  }
  if (problems.length > 0) throw new Error("Invalid configuration:\n- " + problems.join("\n- "));
  return {
    port: int(env, "COLLAB_PORT", DEFAULTS.port),
    address: env.COLLAB_ADDRESS ?? DEFAULTS.address,
    path: env.COLLAB_PATH ?? DEFAULTS.path,
    apiBaseUrl,
    internalSecret,
    allowedOrigins: origins,
    logLevel: (env.COLLAB_LOG_LEVEL ?? "info") as Level,
    debounceMs: int(env, "COLLAB_DEBOUNCE_MS", DEFAULTS.debounceMs),
    maxDebounceMs: int(env, "COLLAB_MAX_DEBOUNCE_MS", DEFAULTS.maxDebounceMs),
    snapshotIntervalMs: int(env, "COLLAB_SNAPSHOT_INTERVAL_MS", DEFAULTS.snapshotIntervalMs),
    leaseRenewMs: int(env, "COLLAB_LEASE_RENEW_MS", DEFAULTS.leaseRenewMs),
    leaseGraceMs: int(env, "COLLAB_LEASE_GRACE_MS", DEFAULTS.leaseGraceMs),
    maxMessageBytes: int(env, "COLLAB_MAX_MESSAGE_BYTES", DEFAULTS.maxMessageBytes),
    maxStateBytes: int(env, "COLLAB_MAX_STATE_BYTES", DEFAULTS.maxStateBytes),
    maxConnectionsPerUser: int(env, "COLLAB_MAX_CONNECTIONS_PER_USER", DEFAULTS.maxConnectionsPerUser),
    maxConnectionsTotal: int(env, "COLLAB_MAX_CONNECTIONS_TOTAL", DEFAULTS.maxConnectionsTotal),
    storeRetries: int(env, "COLLAB_STORE_RETRIES", DEFAULTS.storeRetries),
    storeRetryDelayMs: int(env, "COLLAB_STORE_RETRY_DELAY_MS", DEFAULTS.storeRetryDelayMs),
    storeRecoveryDelayMs: int(env, "COLLAB_STORE_RECOVERY_DELAY_MS", DEFAULTS.storeRecoveryDelayMs),
    apiTimeoutMs: int(env, "COLLAB_API_TIMEOUT_MS", DEFAULTS.apiTimeoutMs),
    limits: DEFAULT_LIMITS,
  };
}
