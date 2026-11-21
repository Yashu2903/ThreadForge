import { useEffect, useMemo, useState } from "react";
import { Api, ApiError } from "./api";
import { RoomView } from "./RoomView";
import { RoomsPage } from "./shell/Home";
import { JoinPage } from "./shell/JoinPage";
import { TOKEN_RE, pendingInvitation, scrubAddressBar } from "./shell/invites";
import type { Me } from "./types";

/** Hash routes (#/, #/rooms/<id>, #/join and #/join/<key>) so no page path can collide with an API prefix on the same origin. */
function useHashRoute(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

type Route = { kind: "room"; id: string } | { kind: "join"; token: string | null } | { kind: "home" };

export function parseRoute(hash: string): Route {
  const room = /^#\/rooms\/([0-9a-fA-F-]{36})$/.exec(hash);
  if (room) return { kind: "room", id: room[1]!.toLowerCase() };
  const join = /^#\/join(?:\/([^/?#]*))?$/.exec(hash);
  if (join) return { kind: "join", token: join[1] && TOKEN_RE.test(join[1]) ? join[1] : null };
  return { kind: "home" };
}

export function App(props: { api?: Api }) {
  const api = useMemo(() => props.api ?? new Api(), [props.api]);
  const route = parseRoute(useHashRoute());
  const [me, setMe] = useState<Me | null | "anonymous">(null);
  // an invitation opened from a link: read once, kept only for the sign-in, and taken out of the address bar at once
  const routeToken = route.kind === "join" ? route.token : null;
  useEffect(() => {
    if (!routeToken) return;
    pendingInvitation.save(routeToken);
    scrubAddressBar("#/join");
  }, [routeToken]);

  useEffect(() => {
    api
      .get<Me>("/auth/me")
      .then((res) => setMe(res.body))
      .catch((err) => setMe(err instanceof ApiError && err.status === 401 ? "anonymous" : "anonymous"));
  }, [api]);

  // back from signing in with an invitation waiting: continue it instead of landing on the room list
  const waiting = me !== null && me !== "anonymous" && route.kind === "home" ? pendingInvitation.read() : null;
  useEffect(() => {
    if (waiting) window.location.hash = "#/join";
  }, [waiting]);

  if (me === null) return <p role="status" className="centered">Loading…</p>;
  if (me === "anonymous") {
    const invited = route.kind === "join" && (route.token || pendingInvitation.read());
    return (
      <main className="signin" data-testid="signin">
        <div className="signin-card">
          <span className="brand-mark" aria-hidden="true">
            TF
          </span>
          <h1>ThreadForge</h1>
          {invited ? (
            <>
              <p>You have been invited to a room. Sign in to see which one and to join it.</p>
              <a className="button primary" href="/auth/login">
                Sign in with Google
              </a>
              <button type="button" className="link" onClick={() => (pendingInvitation.clear(), (window.location.hash = "#/"))}>
                Cancel the invitation
              </button>
            </>
          ) : (
            <>
              <p>Sign in to see your rooms.</p>
              <a className="button primary" href="/auth/login">
                Sign in with Google
              </a>
            </>
          )}
        </div>
      </main>
    );
  }

  if (route.kind === "room") return <RoomView key={route.id} api={api} me={me} roomId={route.id} onBack={() => (window.location.hash = "#/")} />;
  if (route.kind === "join") return <JoinPage key={routeToken ?? "join"} api={api} me={me} token={routeToken ?? pendingInvitation.read()} />;
  return <RoomsPage api={api} me={me} />;
}
