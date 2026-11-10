/** Test data, inserted directly into the test database (the service under test never does this). */
import { createHash, randomBytes, randomUUID } from "node:crypto";
import pg from "pg";
import { DATABASE_URL_PG } from "./env";

export const pool = new pg.Pool({ connectionString: DATABASE_URL_PG, max: 4 });

export interface TestUser {
  id: string;
  name: string;
  sessionId: string;
  secret: string;
  cookie: string;
}

export async function createUser(name = "User"): Promise<TestUser> {
  const secret = randomBytes(32).toString("base64url");
  const user = await pool.query("INSERT INTO users (display_name) VALUES ($1) RETURNING id", [name]);
  const session = await pool.query(
    "INSERT INTO sessions (user_id, secret_hash, expires_at) VALUES ($1, $2, now() + interval '1 day') RETURNING id",
    [user.rows[0].id, createHash("sha256").update(secret).digest("hex")],
  );
  return { id: user.rows[0].id, name, sessionId: session.rows[0].id, secret, cookie: `tf_session=${secret}` };
}

export async function createRoom(owner: TestUser): Promise<string> {
  const room = await pool.query("INSERT INTO rooms (name, owner_id) VALUES ('Room', $1) RETURNING id", [owner.id]);
  await addMember(room.rows[0].id, owner, "owner");
  return room.rows[0].id;
}

export async function addMember(roomId: string, user: TestUser, role: "owner" | "editor" | "viewer"): Promise<void> {
  await pool.query(
    "INSERT INTO memberships (room_id, user_id, role, state) VALUES ($1, $2, $3, 'active')" +
      " ON CONFLICT (room_id, user_id) DO UPDATE SET role = $3, state = 'active', removed_at = NULL",
    [roomId, user.id, role],
  );
}

/** interchange JSON: one paragraph per string */
export function interchange(...texts: string[]): unknown {
  return {
    schema_version: 1,
    blocks: texts.map((t) => ({ type: "paragraph", id: randomUUID(), ...(t ? { children: [{ type: "text", text: t }] } : {}) })),
  };
}

export async function createDocument(roomId: string, owner: TestUser, content: unknown = interchange("Hello"), title = "Plan", seedFormat: "interchange" | "tiptap" = "interchange"): Promise<string> {
  const hash = createHash("sha256").update(JSON.stringify(content)).digest();
  const r = await pool.query(
    "INSERT INTO documents (room_id, created_by, title, template_key, seed_format, content, content_hash, created_at, updated_at, content_updated_at)" +
      " VALUES ($1, $2, $3, $6, $7, $4, $5, now(), now(), now()) RETURNING id",
    [roomId, owner.id, title, JSON.stringify(content), hash, seedFormat === "tiptap" ? "ai_draft" : "blank", seedFormat],
  );
  return r.rows[0].id;
}

export async function createAsset(roomId: string, owner: TestUser, state = "pending"): Promise<string> {
  const id = randomUUID();
  await pool.query(
    "INSERT INTO room_assets (id, room_id, uploaded_by, origin, state, media_type, byte_size, sha256, width_px, height_px, storage_key, created_at, attached_at)" +
      " VALUES ($1, $2, $3, 'editor_upload', $4::varchar, 'image/png', 10, $5, 10, 10, $6, now(), CASE WHEN $4::varchar = 'attached' THEN now() END)",
    [id, roomId, owner.id, state, randomBytes(32), randomUUID().replace(/-/g, "")],
  );
  return id;
}

export async function one<T = Record<string, unknown>>(sql: string, params: unknown[] = []): Promise<T> {
  const r = await pool.query(sql, params);
  return r.rows[0] as T;
}

export async function all<T = Record<string, unknown>>(sql: string, params: unknown[] = []): Promise<T[]> {
  return (await pool.query(sql, params)).rows as T[];
}

/** A room with an owner, an editor, a viewer and a document. */
export async function scene(content: unknown = interchange("Hello")) {
  const owner = await createUser("Owner");
  const editor = await createUser("Editor");
  const viewer = await createUser("Viewer");
  const roomId = await createRoom(owner);
  await addMember(roomId, editor, "editor");
  await addMember(roomId, viewer, "viewer");
  const docId = await createDocument(roomId, owner, content);
  return { owner, editor, viewer, roomId, docId };
}

/**
 * A pending AI edit proposal, inserted straight into the database with the chain of rows it needs (chat message,
 * request, result): the service under test only ever READS and CHANGES the state of proposals, it never makes one.
 */
export async function createProposal(opts: {
  roomId: string;
  documentId: string;
  user: TestUser;
  operations: unknown;
  baseHashes: Record<string, string>;
  scope: { whole_ids?: string[]; ranges?: unknown[]; anchors?: string[] };
  state?: string;
  expiresInSeconds?: number;
}): Promise<string> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const requestId = randomUUID();
    const message = await client.query(
      "INSERT INTO messages (room_id, author_id, client_message_id, body, initial_request_fingerprint, created_at, kind, ai_request_id)" +
        " VALUES ($1, $2, gen_random_uuid(), '@AI test', decode(repeat('00', 32), 'hex'), now(), 'ai_request', $3) RETURNING id",
      [opts.roomId, opts.user.id, requestId],
    );
    await client.query(
      "INSERT INTO ai_requests (id, room_id, requested_by, message_id, client_request_id, request_fingerprint, operation, task_hint," +
        " instruction, document_id, include_document_content, document_scope, include_chat_context, state, key_source_generation," +
        " key_source_embedding, charged_user_id, finished_at)" +
        " VALUES ($1, $2, $3, $4, gen_random_uuid(), decode(repeat('00', 32), 'hex'), 'edit', 'ask', 'test', $5, true, 'edit', false," +
        " 'succeeded', 'server', 'server', $3, now())",
      [requestId, opts.roomId, opts.user.id, message.rows[0].id, opts.documentId],
    );
    const result = await client.query(
      "INSERT INTO ai_results (request_id, room_id, task_type, answer) VALUES ($1, $2, 'edit_proposal', '{}'::jsonb) RETURNING id",
      [requestId, opts.roomId],
    );
    const state = opts.state ?? "pending";
    const proposal = await client.query(
      "INSERT INTO ai_proposals (request_id, result_id, room_id, document_id, kind, state, operations, base_hashes, scope, captured_counter," +
        " created_by, created_at, expires_at, applying_by, applying_since, resolved_at)" +
        " VALUES ($1, $2, $3, $4, 'edit', $5::text, $6, $7, $8, 1, $9, now() - interval '1 hour', now() + ($10 || ' seconds')::interval," +
        " CASE WHEN $5::text = 'applying' THEN $9::uuid END, CASE WHEN $5::text = 'applying' THEN now() - interval '5 minutes' END," +
        " CASE WHEN $5::text NOT IN ('pending', 'applying') THEN now() END) RETURNING id",
      [
        requestId,
        result.rows[0].id,
        opts.roomId,
        opts.documentId,
        state,
        JSON.stringify(opts.operations),
        JSON.stringify(opts.baseHashes),
        JSON.stringify(opts.scope),
        opts.user.id,
        String(opts.expiresInSeconds ?? 3600),
      ],
    );
    await client.query("COMMIT");
    return proposal.rows[0].id;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}
