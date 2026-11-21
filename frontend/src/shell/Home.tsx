import { type ReactNode, useState } from "react";
import type { Api } from "../api";
import { RoomRail, acronym, colourFor, useRooms } from "../components/RoomRail";
import type { Me, RoomSummary } from "../types";
import { IconCompass, IconHome, IconPlus } from "../ui/icons";
import { CreateRoomDialog, JoinRoomDialog } from "./Dialogs";
import { UserPanel } from "./UserPanel";

const ROLE_LABEL: Record<string, string> = { owner: "Owner", editor: "Editor", viewer: "Viewer" };

/**
 * The application shell of every page that is not a room: the same room rail and sidebar as inside a room, so the layout never
 * changes under the person's hands, with the account area in the same place (bottom of the sidebar). Rooms come from the server;
 * nothing here is sample data.
 */
export function HomeShell(props: { api: Api; me: Me; active: "home" | "join"; rooms?: ReturnType<typeof useRooms>; children: ReactNode | ((actions: { create: () => void; join: () => void }) => ReactNode); onCreated?: (room: RoomSummary) => void }) {
  const own = useRooms(props.api);
  const list = props.rooms ?? own;
  const [dialog, setDialog] = useState<"create" | "join" | null>(null);

  function created(room: RoomSummary) {
    setDialog(null);
    list.reload();
    props.onCreated?.(room);
    window.location.hash = `#/rooms/${room.id}`;
  }

  return (
    <div className="app-shell home-shell">
      <RoomRail rooms={list.rooms} onHome={() => (window.location.hash = "#/")} onCreate={() => setDialog("create")} onJoin={() => setDialog("join")} homeCurrent={props.active === "home"} />
      <nav className="sidebar" aria-label="Navigation">
        <div className="sidebar-head">
          <h1 className="room-title">ThreadForge</h1>
        </div>
        <div className="sidebar-scroll">
          <ul className="nav-list">
            <li>
              <a href="#/" className={props.active === "home" ? "selected" : ""} aria-current={props.active === "home" ? "page" : undefined}>
                <IconHome /> <span>Your rooms</span>
              </a>
            </li>
            <li>
              <button type="button" className="nav-button" onClick={() => setDialog("create")}>
                <IconPlus /> <span>Create a room</span>
              </button>
            </li>
            <li>
              <button type="button" className={`nav-button${props.active === "join" ? " selected" : ""}`} onClick={() => setDialog("join")}>
                <IconCompass /> <span>Join a room</span>
              </button>
            </li>
          </ul>
        </div>
        <UserPanel api={props.api} me={props.me} detail={props.me.email ?? "Signed in"} />
      </nav>
      <main className="home-main" aria-label="Main">
        {typeof props.children === "function" ? props.children({ create: () => setDialog("create"), join: () => setDialog("join") }) : props.children}
      </main>
      {dialog === "create" && <CreateRoomDialog api={props.api} onCreated={created} onClose={() => setDialog(null)} />}
      {dialog === "join" && <JoinRoomDialog onClose={() => setDialog(null)} />}
    </div>
  );
}

export function RoomsPage(props: { api: Api; me: Me }) {
  const rooms = useRooms(props.api);
  return (
    <HomeShell api={props.api} me={props.me} active="home" rooms={rooms}>
      {({ create, join }) => (
      <div className="page">
        <header className="page-header">
          <div>
            <h1>Your rooms</h1>
            <p className="muted">Signed in as {props.me.display_name}</p>
          </div>
          <div className="page-actions">
            <button type="button" onClick={join}>
              <IconCompass size={16} /> Join a room
            </button>
            <button type="button" className="primary" onClick={create}>
              <IconPlus size={16} /> Create a room
            </button>
          </div>
        </header>
        {!rooms.loaded && (
          <p role="status" className="muted">
            Loading your rooms…
          </p>
        )}
        {rooms.failed && (
          <p role="alert" className="error">
            Could not load your rooms. <button type="button" className="link" onClick={rooms.reload}>Try again</button>
          </p>
        )}
        {rooms.loaded && !rooms.failed && rooms.rooms.length === 0 && (
          <section className="empty-state" aria-label="No rooms yet">
            <div className="empty-art" aria-hidden="true">
              <IconHome size={40} />
            </div>
            <h2>You are not in any room yet</h2>
            <p className="muted">Create a room for your team, or join one with the invitation link or key someone sent you.</p>
            <div className="actions center">
              <button type="button" className="primary" onClick={create}>
                <IconPlus size={16} /> Create a room
              </button>
              <button type="button" onClick={join}>
                <IconCompass size={16} /> Join a room
              </button>
            </div>
          </section>
        )}
        {rooms.rooms.length > 0 && (
          <ul className="room-grid" aria-label="Rooms">
            {rooms.rooms.map((room) => (
              <li key={room.id}>
                <a className="room-card" href={`#/rooms/${room.id}`}>
                  <span className="room-card-icon" style={{ background: colourFor(room.id) }} aria-hidden="true">
                    {acronym(room.name)}
                  </span>
                  <span className="room-card-text">
                    <strong title={room.name}>{room.name}</strong>
                    <span className={`role-chip role-${room.role}`}>{ROLE_LABEL[room.role] ?? room.role}</span>
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
      )}
    </HomeShell>
  );
}
