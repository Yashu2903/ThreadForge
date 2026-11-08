/** Entry point: configuration from the environment, start, and shut down gracefully on SIGTERM/SIGINT. */
import { configFromEnv, type CollabConfig } from "./config.js";
import { CollabServer } from "./server.js";

let config: CollabConfig;
try {
  config = configFromEnv();
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}
`);
  process.exit(2);
}
const service = new CollabServer(config);

let stopping = false;
const shutdown = async (signal: string): Promise<void> => {
  if (stopping) return;
  stopping = true;
  process.stdout.write(JSON.stringify({ level: "info", msg: "signal received", signal }) + "\n");
  const timer = setTimeout(() => process.exit(1), 25_000); // never hang past the orchestrator's kill
  timer.unref();
  await service.stop();
  process.exit(0);
};
process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));

service.start().catch((error: unknown) => {
  process.stderr.write(`failed to start: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(1);
});
