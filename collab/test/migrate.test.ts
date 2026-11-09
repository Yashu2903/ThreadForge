/**
 * The operator command `python -m app.commands.migrate_documents_to_collab`, run for real against the
 * real API, PostgreSQL and a running collaboration service.
 */
import { execFile } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";
import { BACKEND_DIR, DATABASE_URL_ASYNC, INTERNAL_SECRET } from "./env";
import { all, createDocument, interchange, one, pool, scene } from "./fixtures";
import { connect, docText, startCollab, stopAll } from "./harness";

afterEach(stopAll);

function runCommand(args: string[], serviceUrl: string, secret = INTERNAL_SECRET): Promise<{ code: number; out: string; err: string }> {
  return new Promise((resolve) => {
    execFile(
      "uv",
      ["run", "python", "-m", "app.commands.migrate_documents_to_collab", ...args],
      { cwd: BACKEND_DIR, env: { ...process.env, DATABASE_URL: DATABASE_URL_ASYNC, INTERNAL_API_SECRET: secret, COLLAB_SERVICE_URL: serviceUrl }, timeout: 120_000 },
      (error, stdout, stderr) => resolve({ code: error ? ((error as { code?: number }).code ?? 1) : 0, out: stdout, err: stderr }),
    );
  });
}

const stateOf = (id: string) => one<{ collab_state: string }>("SELECT collab_state FROM documents WHERE id = $1", [id]);
const hasYjs = async (id: string) => (await all("SELECT 1 FROM document_yjs_state WHERE document_id = $1", [id])).length === 1;
const alien = { schema_version: 1, blocks: [{ type: "alien", id: "00000000-0000-4000-8000-000000000001" }] };

describe("migrate_documents_to_collab", () => {
  it("a dry run validates every seed and changes nothing", async () => {
    const s = await scene(interchange("Alpha"));
    const bad = await createDocument(s.roomId, s.owner, alien, "Broken");
    const server = await startCollab();
    const { code, out } = await runCommand(["--dry-run", "--room", s.roomId], `http://127.0.0.1:${server.port}`);
    expect(out).toContain(`${s.docId}  would_migrate`);
    expect(out).toMatch(new RegExp(`${bad}  invalid  adapt_failed`));
    expect(code).toBe(1); // an invalid seed is a failure to act on
    expect((await stateOf(s.docId)).collab_state).toBe("legacy");
    expect(await hasYjs(s.docId)).toBe(false);
    expect(await all("SELECT 1 FROM document_snapshots WHERE document_id = $1", [s.docId])).toHaveLength(0);
  });

  it("migrates the good documents, leaves the invalid one legacy, and never touches documents.content", async () => {
    const s = await scene(interchange("Migrate me", "second paragraph"));
    const bad = await createDocument(s.roomId, s.owner, alien, "Broken");
    const before = await one<{ content: unknown; content_hash: Buffer }>("SELECT content, content_hash FROM documents WHERE id = $1", [s.docId]);
    const server = await startCollab();
    const { code, out } = await runCommand(["--room", s.roomId], `http://127.0.0.1:${server.port}`);
    expect(out).toContain(`${s.docId}  migrated`);
    expect(out).toContain(`${bad}  invalid`);
    expect(out).toMatch(/summary: invalid=1, migrated=1/);
    expect(code).toBe(1);

    // the migrated document: ready, Yjs state stored, seed snapshot written
    expect((await stateOf(s.docId)).collab_state).toBe("ready");
    expect(await hasYjs(s.docId)).toBe(true);
    const snaps = await all<{ kind: string }>("SELECT kind FROM document_snapshots WHERE document_id = $1", [s.docId]);
    expect(snaps.map((r) => r.kind)).toContain("seed");
    expect(await one("SELECT content, content_hash FROM documents WHERE id = $1", [s.docId])).toEqual(before);
    // the broken one stays exactly as it was: legacy, no state, NOT needs_repair
    expect((await stateOf(bad)).collab_state).toBe("legacy");
    expect(await hasYjs(bad)).toBe(false);

    // a person opening the migrated document sees the text
    const client = connect(server, s.owner, s.docId);
    await client.ready;
    expect(docText(client.ydoc)).toBe("Migrate me\nsecond paragraph");
  });

  it("is idempotent: a second run finds nothing to do", async () => {
    const s = await scene(interchange("Once"));
    const server = await startCollab();
    const url = `http://127.0.0.1:${server.port}`;
    const first = await runCommand(["--room", s.roomId], url);
    expect(first.out).toContain("migrated");
    const counter = (await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [s.docId])).update_counter;
    const second = await runCommand(["--room", s.roomId], url);
    expect(second.code).toBe(0);
    expect(second.out).toContain("summary: nothing to do");
    expect((await one<{ update_counter: string }>("SELECT update_counter FROM document_yjs_state WHERE document_id = $1", [s.docId])).update_counter).toBe(counter);
    expect(await all("SELECT 1 FROM document_snapshots WHERE document_id = $1 AND kind = 'seed'", [s.docId])).toHaveLength(1);
  });

  it("--document limits it to one document; a needs_repair document is skipped", async () => {
    const s = await scene(interchange("One"));
    const two = await createDocument(s.roomId, s.owner, interchange("Two"), "Two");
    const repair = await createDocument(s.roomId, s.owner, interchange("Three"), "Three");
    await pool.query("UPDATE documents SET collab_state = 'needs_repair' WHERE id = $1", [repair]);
    const server = await startCollab();
    const url = `http://127.0.0.1:${server.port}`;
    const only = await runCommand(["--document", two], url);
    expect(only.out).toContain(`${two}  migrated`);
    expect((await stateOf(s.docId)).collab_state).toBe("legacy");
    const rest = await runCommand(["--room", s.roomId], url);
    expect(rest.out).toContain(`${s.docId}  migrated`);
    expect(rest.out).not.toContain(repair); // needs_repair is not legacy: not even listed
    expect((await stateOf(repair)).collab_state).toBe("needs_repair");
  });

  it("is safe while people are editing the same document", async () => {
    const s = await scene(interchange("Shared"));
    const server = await startCollab();
    const client = connect(server, s.owner, s.docId); // opening it already seeds it
    await client.ready;
    const { out } = await runCommand(["--document", s.docId], `http://127.0.0.1:${server.port}`);
    expect(out).toContain("summary: nothing to do"); // already migrated by the open
    expect(docText(client.ydoc)).toBe("Shared");
  });

  it("refuses a wrong secret and reports an unreachable service", async () => {
    const s = await scene(interchange("Locked"));
    const server = await startCollab();
    const wrong = await runCommand(["--document", s.docId], `http://127.0.0.1:${server.port}`, "wrong-secret-0123456789");
    expect(wrong.out).toContain("refused the secret");
    expect(wrong.code).toBe(1);
    expect((await stateOf(s.docId)).collab_state).toBe("legacy");
    const down = await runCommand(["--document", s.docId], "http://127.0.0.1:1");
    expect(down.code).toBe(2);
    expect(down.err).toContain("not reachable");
  });

  it("the service route needs the secret and a well-formed body", async () => {
    const server = await startCollab();
    const url = `http://127.0.0.1:${server.port}/internal/migrate`;
    const post = (headers: Record<string, string>, body: string) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body });
    expect((await post({}, "{}")).status).toBe(401);
    expect((await post({ "X-Internal-Secret": "nope" }, "{}")).status).toBe(401);
    expect((await post({ "X-Internal-Secret": INTERNAL_SECRET }, "not json")).status).toBe(422);
    expect((await post({ "X-Internal-Secret": INTERNAL_SECRET }, JSON.stringify({ document_id: "../../etc" }))).status).toBe(422);
    const unknown = await post({ "X-Internal-Secret": INTERNAL_SECRET }, JSON.stringify({ document_id: "11111111-1111-4111-8111-111111111111" }));
    expect(((await unknown.json()) as { result: string }).result).toBe("failed");
  });
});
