export * from "./protocol.js";
export { DEFAULTS, RoomClient, reactionFor } from "./room-client.js";
export type { Deps, Options, ResponseLike, SocketLike, State, Tunables } from "./room-client.js";

import type { Deps, SocketLike } from "./room-client.js";

/** Real dependencies for a browser: native WebSocket and fetch (cookies sent with credentials). */
export function browserDeps(): Deps {
  return {
    createSocket: (url) => new WebSocket(url) as unknown as SocketLike,
    fetch: (url, init) => fetch(url, { ...init, credentials: "include" }),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    now: () => Date.now(),
    random: () => Math.random(),
  };
}
