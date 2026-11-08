// Drives the REAL RoomClient (built to ../dist) against a REAL backend, for end-to-end tests.
// Environment: TF_BASE_URL, TF_ORIGIN, TF_COOKIE, TF_ROOM, TF_USER (and optional TF_COOKIE_NAME).
// Prints one JSON line whenever the client state changes. Reads commands from stdin, one per line:
//   send <text> | typing | avail <available|busy|dnd> | drop | stop
// `drop` kills the TCP connection without a close handshake (an abrupt network failure).
import { createRequire } from "node:module";
import readline from "node:readline";
import { RoomClient } from "../dist/index.js";

const require = createRequire(import.meta.url);
const WebSocket = require("ws");

const env = process.env;
const headers = { Cookie: `${env.TF_COOKIE_NAME ?? "tf_session"}=${env.TF_COOKIE}`, Origin: env.TF_ORIGIN };
let current = null;

const deps = {
  createSocket: (url) => {
    const ws = new WebSocket(url, { headers });
    current = ws;
    const socket = { send: (d) => ws.send(d), close: (c) => ws.close(c), onopen: null, onmessage: null, onclose: null, onerror: null };
    ws.on("open", () => socket.onopen?.());
    ws.on("message", (data) => socket.onmessage?.({ data: data.toString() }));
    ws.on("close", (code) => socket.onclose?.({ code }));
    // like a browser: a refused handshake is just an abnormal close (1006), the status is not visible
    ws.on("unexpected-response", () => socket.onclose?.({ code: 1006 }));
    ws.on("error", () => socket.onerror?.());
    return socket;
  },
  fetch: (url, init = {}) => fetch(url, { ...init, headers: { ...init.headers, ...headers } }),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h),
  now: () => Date.now(),
  random: () => Math.random(),
};

const client = new RoomClient(deps, {
  roomId: env.TF_ROOM,
  userId: env.TF_USER,
  httpBase: env.TF_BASE_URL,
  wsBase: env.TF_BASE_URL.replace(/^http/, "ws"),
  tunables: { reconnectInitialMs: Number(env.TF_RECONNECT_MS ?? 200), reconnectMaxMs: Number(env.TF_RECONNECT_MS ?? 1000) },
});

let last = "";
function snapshot() {
  const s = client.state;
  return {
    status: s.status,
    terminal: s.terminalReason,
    cursor: s.cursor,
    room: s.room?.name ?? null,
    members: s.members.map((m) => m.user_id),
    messages: [...s.messages.values()].map((m) => ({ id: m.id, v: m.version, body: m.body, deleted: m.is_deleted })),
    online: s.presence.online.map((o) => `${o.user_id}:${o.availability}`),
    presenceDegraded: s.presence.degraded,
    typing: s.typing.userIds,
    pending: client.pendingSendIds.length,
  };
}
client.onChange(() => {
  const line = JSON.stringify(snapshot());
  if (line !== last) {
    last = line;
    console.log(line);
  }
});
client.start();

readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const [cmd, ...rest] = line.trim().split(" ");
  if (cmd === "send") void client.sendMessage(rest.join(" ")).then((m) => console.log(JSON.stringify({ sent: m?.id ?? null })));
  else if (cmd === "typing") client.userTyping();
  else if (cmd === "avail") client.setAvailability(rest[0]);
  else if (cmd === "drop") current?._socket?.destroy();
  else if (cmd === "stop") {
    client.stop();
    process.exit(0);
  }
});
