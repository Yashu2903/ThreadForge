import { useEffect, useMemo, useState } from "react";
import { Api, ApiError } from "./api";
import { RoomView } from "./RoomView";
import type { Me, Page, RoomSummary } from "./types";

/** Hash routes (#/ and #/rooms/<id>) so no page path can collide with an API prefix on the same origin. */
function useHashRoute(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

export function App(props: { api?: Api }) {
  const api = useMemo(() => props.api ?? new Api(), [props.api]);
  const route = useHashRoute();
  const [me, setMe] = useState<Me | null | "anonymous">(null);

  useEffect(() => {
    api
      .get<Me>("/auth/me")
      .then((res) => setMe(res.body))
      .catch((err) => setMe(err instanceof ApiError && err.status === 401 ? "anonymous" : "anonymous"));
  }, [api]);

  if (me === null) return <p role="status" className="centered">Loading…</p>;
  if (me === "anonymous") {
    return (
      <main className="centered">
        <h1>ThreadForge</h1>
        <p>Sign in to see your rooms.</p>
        <a className="button primary" href="/auth/login">
          Sign in with Google
        </a>
      </main>
    );
  }

  const match = /^#\/rooms\/([0-9a-fA-F-]{36})$/.exec(route);
  if (match) {
    return <RoomView key={match[1]} api={api} me={me} roomId={match[1]!.toLowerCase()} onBack={() => (window.location.hash = "#/")} />;
  }
  return <RoomsPage api={api} me={me} />;
}

function RoomsPage({ api, me }: { api: Api; me: Me }) {
  const [rooms, setRooms] = useState<RoomSummary[] | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const res = await api.get<Page<RoomSummary>>("/rooms?limit=100");
      setRooms(res.body.items);
    } catch {
      setError("Could not load your rooms.");
    }
  }
  useEffect(() => {
    void load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    try {
      const res = await api.send<RoomSummary>("POST", "/rooms", { name: name.trim() });
      setName("");
      window.location.hash = `#/rooms/${res.body.id}`;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
    }
  }

  return (
    <main className="rooms-page">
      <h1>Your rooms</h1>
      <p className="muted">Signed in as {me.display_name}</p>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <ul className="room-list">
        {rooms?.map((room) => (
          <li key={room.id}>
            <a href={`#/rooms/${room.id}`}>{room.name}</a> <span className="muted">({room.role})</span>
          </li>
        ))}
      </ul>
      {rooms?.length === 0 && <p>You are not in any room yet.</p>}
      <form onSubmit={create} className="inline-form">
        <label>
          <span className="sr-only">New room name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New room name" maxLength={120} />
        </label>
        <button type="submit" className="primary">
          Create room
        </button>
      </form>
    </main>
  );
}
