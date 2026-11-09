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
