import { useEffect, useState } from "react";
import type { Api } from "../api";
import type { Page, RoomSummary } from "../types";

/** A stable colour for a name or id (Discord gives every account a coloured avatar). */
export function colourFor(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i += 1) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 45% 42%)`;
}

export function initialOf(name: string): string {
  const t = name.trim();
  return t ? [...t][0]!.toUpperCase() : "?";
}

/** The letters shown in a room's round icon: first letters of up to three words. */
export function acronym(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return [...words[0]!].slice(0, 2).join("").toUpperCase();
  return words
    .slice(0, 3)
    .map((w) => [...w][0]!.toUpperCase())
    .join("");
}

/** The rooms the person belongs to; failing to load them only leaves the rail short. */
export function useRoomList(api: Api, refreshKey?: unknown): RoomSummary[] {
  const [rooms, setRooms] = useState<RoomSummary[]>([]);
  useEffect(() => {
    let cancelled = false;
    api
      .get<Page<RoomSummary>>("/rooms?limit=100")
      .then((res) => !cancelled && setRooms(res.body.items))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [api, refreshKey]);
  return rooms;
}

/**
 * The far-left strip of round room icons (Discord's server list). A room is a link, so it works with the
 * existing hash routes; the first button goes back to the list of rooms.
 */
export function RoomRail(props: { rooms: RoomSummary[]; currentId?: string; onHome: () => void }) {
  return (
    <nav className="room-rail" aria-label="Your rooms">
      <button type="button" className="rail-icon rail-home" aria-label="Back to your rooms" title="Your rooms" onClick={props.onHome}>
        <span aria-hidden="true">TF</span>
      </button>
      <hr className="rail-sep" />
      {props.rooms.map((room) => (
        <a
          key={room.id}
          href={`#/rooms/${room.id}`}
          className={`rail-icon${room.id === props.currentId ? " current" : ""}`}
          aria-label={room.name}
          aria-current={room.id === props.currentId ? "page" : undefined}
          title={room.name}
          style={room.id === props.currentId ? undefined : { ["--icon-colour" as string]: colourFor(room.id) }}
        >
          <span aria-hidden="true">{acronym(room.name)}</span>
        </a>
      ))}
    </nav>
  );
}
