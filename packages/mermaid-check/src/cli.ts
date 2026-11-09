/**
 * The Mermaid parse check, as a command-line filter:
 *
 *   echo '{"diagrams":[{"id":0,"source":"flowchart TD\n A-->B"}]}' | node dist/cli.js
 *   -> {"results":[{"id":0,"ok":true,"message":""}]}
 *
 * Started by the AI worker (backend/app/ai/answer/mermaid.py) as a child process with an almost empty
 * environment, no network access needed (and none used), and a wall-clock deadline enforced by the
 * parent. Each diagram is checked FIRST with the static rules of the shared schema package (allowed
 * types, size, no directives, no click/href, no HTML, no URLs) and only then handed to the real Mermaid
 * parser, which needs a DOM: jsdom provides one. Nothing is rendered and nothing is evaluated.
 */
import { validateMermaidSource } from "@threadforge/editor-schema";
import { JSDOM } from "jsdom";

interface Request {
  diagrams: { id: number; source: string }[];
}
interface Row {
  id: number;
  ok: boolean;
  message: string;
}

const MAX_INPUT = 4_000_000;
const MAX_DIAGRAMS = 40;

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

function oneLine(value: unknown): string {
  return String((value as { message?: string })?.message ?? value)
    .split("\n")
    .slice(0, 3)
    .join(" | ")
    .slice(0, 300);
}

async function main(): Promise<void> {
  const request = JSON.parse(await readStdin()) as Request;
  const diagrams = request.diagrams.slice(0, MAX_DIAGRAMS);
  const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
  Object.assign(globalThis, { window: dom.window, document: dom.window.document });
  Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
  const { default: mermaid } = await import("mermaid");
  mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });

  const results: Row[] = [];
  for (const { id, source } of diagrams) {
    const rules = validateMermaidSource(source);
    if (!rules.ok) {
      results.push({ id, ok: false, message: rules.message });
      continue;
    }
    try {
      await mermaid.parse(source);
      results.push({ id, ok: true, message: "" });
    } catch (error) {
      results.push({ id, ok: false, message: oneLine(error) });
    }
  }
  process.stdout.write(`${JSON.stringify({ results })}\n`);
}

main().then(
  () => process.exit(0),
  () => {
    process.stdout.write(`${JSON.stringify({ results: [] })}\n`);
    process.exit(1);
  },
);
