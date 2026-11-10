/**
 * Command-line filter around check(): one JSON request on stdin, one JSON response on stdout.
 *
 * Started by the AI worker (backend/app/ai/edit/check.py) as a child process with an almost empty
 * environment and a wall-clock deadline enforced by the parent. It reads nothing but stdin and writes
 * nothing but stdout; there is no database, no network, no file access.
 */
import { type CheckRequest, check } from "./check.js";

const MAX_INPUT = 6_000_000;

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += (chunk as Buffer).length;
    if (size > MAX_INPUT) throw new Error("input too large");
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString("utf-8");
}

async function main(): Promise<void> {
  const request = JSON.parse(await readStdin()) as CheckRequest;
  process.stdout.write(`${JSON.stringify(await check(request))}\n`);
}

main().then(
  () => process.exit(0),
  () => {
    process.stdout.write(`${JSON.stringify({ ok: false, errors: [{ op: null, code: "check_crashed", message: "the check could not run" }] })}\n`);
    process.exit(1);
  },
);
