import { useCallback, useEffect, useState } from "react";
import type { Api } from "../api";
import type { Page, RoomSummary } from "../types";
import { Tip } from "../ui/Popover";
import { IconCompass, IconPlus } from "../ui/icons";

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

/** The rooms the person belongs to, with a way to load them again (after creating or joining one). Failing to load only leaves the rail short. */
export function useRooms(api: Api, refreshKey?: unknown): { rooms: RoomSummary[]; loaded: boolean; failed: boolean; reload: () => void } {
  const [rooms, setRooms] = useState<RoomSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    api
      .get<Page<RoomSummary>>("/rooms?limit=100")
      .then((res) => {
        if (cancelled) return;
        setRooms(res.body.items);
        setFailed(false);
        setLoaded(true);
      })
      .catch(() => {
        if (cancelled) return;
        setFailed(true);
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [api, refreshKey, tick]);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { rooms, loaded, failed, reload };
}

export function useRoomList(api: Api, refreshKey?: unknown): RoomSummary[] {
  return useRooms(api, refreshKey).rooms;
}

/**
 * The far-left strip of round room icons (Discord's server list). A room is a link, so it works with the hash routes; the
 * first button goes to the list of rooms; the last two start the two ways of getting a room: create one, or join one with an
 * invitation. Every icon has its name in a tooltip (hover and keyboard focus) and as its accessible name.
 */
export function RoomRail(props: { rooms: RoomSummary[]; currentId?: string; onHome: () => void; onCreate?: () => void; onJoin?: () => void; homeCurrent?: boolean }) {
  return (
    <nav className="room-rail" aria-label="Your rooms">
      <Tip label="Your rooms">
        <button type="button" className={`rail-icon rail-home${props.homeCurrent ? " current" : ""}`} aria-label="Back to your rooms" onClick={props.onHome}>
          <span aria-hidden="true">TF</span>
        </button>
      </Tip>
      <hr className="rail-sep" />
      <div className="rail-rooms">
        {props.rooms.map((room) => (
          <Tip key={room.id} label={room.name}>
            <a
              href={`#/rooms/${room.id}`}
              className={`rail-icon${room.id === props.currentId ? " current" : ""}`}
              aria-label={room.name}
              aria-current={room.id === props.currentId ? "page" : undefined}
              style={room.id === props.currentId ? undefined : { ["--icon-colour" as string]: colourFor(room.id) }}
            >
              <span aria-hidden="true">{acronym(room.name)}</span>
            </a>
          </Tip>
        ))}
      </div>
      {(props.onCreate || props.onJoin) && <hr className="rail-sep" />}
      {props.onCreate && (
        <Tip label="Create a room">
          <button type="button" className="rail-icon rail-action" aria-label="Create a room" onClick={props.onCreate}>
            <IconPlus size={22} />
          </button>
        </Tip>
      )}
      {props.onJoin && (
        <Tip label="Join a room">
          <button type="button" className="rail-icon rail-action" aria-label="Join a room" onClick={props.onJoin}>
            <IconCompass size={22} />
          </button>
        </Tip>
      )}
    </nav>
  );
}
