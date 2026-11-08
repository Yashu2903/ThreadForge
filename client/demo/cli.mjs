// A command-line room client for demonstrations (Node 22, the `ws` package for custom headers).
//
//   cd client && npm run build
//   $env:TF_COOKIE = Read-Host "session cookie value" -MaskInput     # never put it on a command line
//   $env:TF_ROOM = "<room id>"; $env:TF_USER = "<your user id>"
//   node demo/cli.mjs
//
// Environment: TF_BASE_URL (default http://127.0.0.1:8000), TF_ORIGIN (default http://localhost:8000,
// must be in the server's TRUSTED_ORIGINS), TF_COOKIE_NAME (default tf_session), TF_AVAILABILITY,
// TF_SEND="text" to send one message once live. The cookie value is never printed.
import { createRequire } from "node:module";
import { RoomClient } from "../dist/index.js";

const require = createRequire(import.meta.url);
const WebSocket = require("ws");

const base = process.env.TF_BASE_URL ?? "http://127.0.0.1:8000";
const origin = process.env.TF_ORIGIN ?? "http://localhost:8000";
const cookieName = process.env.TF_COOKIE_NAME ?? "tf_session";
const cookie = process.env.TF_COOKIE;
const room = process.env.TF_ROOM;
const user = process.env.TF_USER;
if (!cookie || !room || !user) {
  console.error("Set TF_COOKIE, TF_ROOM and TF_USER (see the top of this file).");
  process.exit(2);
}
const headers = { Cookie: `${cookieName}=${cookie}`, Origin: origin };

const deps = {
  createSocket: (url) => {
    const ws = new WebSocket(url, { headers });
    const socket = { send: (d) => ws.send(d), close: (c) => ws.close(c), onopen: null, onmessage: null, onclose: null, onerror: null };
    ws.on("open", () => socket.onopen?.());
    ws.on("message", (data) => socket.onmessage?.({ data: data.toString() }));
    ws.on("close", (code) => socket.onclose?.({ code }));
    ws.on("unexpected-response", () => socket.onclose?.({ code: 1006 })); // like a browser
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
  roomId: room,
  userId: user,
  httpBase: base,
  wsBase: base.replace(/^http/, "ws"),
});

let lastLine = "";
client.onChange(() => {
  const s = client.state;
  const online = s.presence.degraded ? "unknown" : s.presence.online.map((o) => `${o.user_id.slice(0, 4)}:${o.availability}`).join(",");
  const typing = s.typing.degraded ? "unknown" : s.typing.userIds.map((u) => u.slice(0, 4)).join(",");
  const line = `status=${s.status}${s.terminalReason ? `(${s.terminalReason})` : ""} cursor=${s.cursor} messages=${s.messages.size} members=${s.members.length} online=[${online}] typing=[${typing}]`;
  if (line !== lastLine) {
    lastLine = line;
    console.log(line);
  }
});

client.start();
if (process.env.TF_AVAILABILITY) setTimeout(() => client.setAvailability(process.env.TF_AVAILABILITY), 1500);
if (process.env.TF_SEND) setTimeout(() => client.sendMessage(process.env.TF_SEND).then((m) => console.log("sent", m?.id ?? "(pending, will retry)")), 2000);
process.on("SIGINT", () => {
  client.stop();
  process.exit(0);
});
