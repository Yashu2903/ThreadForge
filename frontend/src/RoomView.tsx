import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type Api, ApiError } from "./api";
import { AiSettingsDialog, SourcePanel } from "./ai/Panels";
import { ChatPanel } from "./components/ChatPanel";
import { OpenDialog } from "./components/OpenDialog";
import { RecoveryPanel } from "./components/RecoveryPanel";
import { RoomRail, useRooms } from "./components/RoomRail";
import type { RecoveryRecord } from "./collab/recovery";
import { StartDialog } from "./components/StartDialog";
import type { Capabilities, DocumentFull, DocumentSummary, Me } from "./types";
import { DocumentEditor, type LiveHandle } from "./DocumentEditor";
import { InviteDialog } from "./shell/InviteDialog";
import { CreateRoomDialog, JoinRoomDialog } from "./shell/Dialogs";
import { MembersPanel } from "./shell/MembersPanel";
import { UserPanel } from "./shell/UserPanel";
import { Menu, Tip } from "./ui/Popover";
import { IconChat, IconChevronDown, IconDoc, IconEdit, IconMenu, IconMore, IconPlus, IconSettings, IconUserPlus, IconUsers } from "./ui/icons";
import { useAi, useClientState, useRoomHandles, useStore } from "./useRoom";

/** Phones and tablets can view documents but editing is not supported there. */
export function useIsMobile(): boolean {
  const query = "(pointer: coarse) and (max-width: 900px)";
  const [mobile, setMobile] = useState(() => typeof matchMedia === "function" && matchMedia(query).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const list = matchMedia(query);
    const on = () => setMobile(list.matches);
    list.addEventListener("change", on);
    return () => list.removeEventListener("change", on);
  }, []);
  return mobile;
}

/** A number kept in this browser (the chat width), always inside [min, max]. Unavailable storage just means it is not remembered. */
function useStoredNumber(key: string, fallback: number, min: number, max: number): [number, (n: number) => void] {
  const [value, setValue] = useState(() => {
    try {
      const n = Number(window.localStorage.getItem(key));
      return Number.isFinite(n) && n >= min && n <= max && n !== 0 ? n : fallback;
    } catch {
      return fallback;
    }
  });
  const set = useCallback(
    (n: number) => {
      const clamped = Math.max(min, Math.min(max, Math.round(n)));
      setValue(clamped);
      try {
        window.localStorage.setItem(key, String(clamped));
      } catch {
        /* not remembered */
      }
    },
    [key, min, max],
  );
  return [value, set];
}

const CHAT_MIN = 300;
const CHAT_MAX = 520;
const CHAT_DEFAULT = 360;

/**
 * The room: the room rail, the sidebar (room menu, documents, files, the account), the document in the middle and chat on the right.
 * One RoomClient feeds both the chat and the document events; the DocumentStore holds the list and the open document's METADATA; the
 * DocumentEditor holds the live connection to the text. A client with nothing unsynced follows the room's active document when the owner
 * changes it.
 *
 * Layout (styles.css): at 1200px and up all four panels sit side by side; between 901 and 1199px the sidebar becomes a drawer opened with
 * the menu button; at 900px and below the rail and sidebar share one drawer and Document and Chat are two tabs. Members open in a drawer
 * on top of the chat column at every size, never as a fourth permanent column. Every panel stays mounted when it is hidden, so the open
 * document's connection and an unsent message are never lost by switching tabs or opening a drawer.
 *
 * Mobile editing: on a touch device (a coarse pointer) of 900px or less the document is view-only; a narrow desktop window is not a
 * phone and can still edit.
 */
export function RoomView(props: { api: Api; me: Me; roomId: string; onBack: () => void }) {
  const { api, me, roomId } = props;
  const { client, store, ai } = useRoomHandles(api, roomId, me.id);
  const state = useClientState(client);
  const aiSnap = useAi(ai);
  const [aiDialog, setAiDialog] = useState(false);
  const docs = useStore(store);
  const [dialog, setDialog] = useState<"start" | "open" | "invite" | "create" | "join" | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const liveRef = useRef<LiveHandle | null>(null);
  const rescued = useRef<string | null>(null);
  const [atRisk, setAtRisk] = useState(0);
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null);
  const [recovered, setRecovered] = useState<string | null>(null);
  const mobile = useIsMobile();
  const rooms = useRooms(api);
  // panels
  const [navOpen, setNavOpen] = useState(false); // the sidebar drawer (up to 1199px) / the rail+sidebar drawer (up to 900px)
  const [membersOpen, setMembersOpen] = useState(false);
  const [tab, setTab] = useState<"document" | "chat">("document"); // the narrow layout's two tabs
  const [chatHidden, setChatHidden] = useState(false); // desktop: the chat column collapsed
  const [chatWidth, setChatWidth] = useStoredNumber("threadforge.chatWidth", CHAT_DEFAULT, CHAT_MIN, CHAT_MAX);
  const [invitesTick, setInvitesTick] = useState(0);
  const [statusSlot, setStatusSlot] = useState<HTMLElement | null>(null);

  const role = state.role;
  const isOwner = role === "owner";
  const activeId = state.room?.active_document_id ?? null;
  const online = useMemo(() => new Set(state.presence.online.map((p) => p.user_id)), [state.presence.online]);

  useEffect(() => {
    let cancelled = false;
    api
      .get<Capabilities>("/capabilities")
      .then((res) => !cancelled && setCapabilities(res.body))
      .catch(() => undefined); // the Drive explanation simply does not show
    return () => {
      cancelled = true;
    };
  }, [api]);

  // Follow the room's active document. A client with NOTHING UNSYNCED opens the new one; a client
  // whose last changes the server has not acknowledged yet keeps its document, says the room
  // switched, and lets the user choose (never replaced silently). Once the server catches up
  // (atRisk reaches 0) the pending switch happens by itself.
  const followed = useRef<string | null>(null);
  const stayed = useRef<string | null>(null); // the user chose "Stay here" for this active document
  useEffect(() => {
    if (!activeId) return;
    if (followed.current === activeId) return setPendingSwitch(null);
    if (stayed.current === activeId) return;
    const live = liveRef.current;
    if (live && live.docId !== activeId && atRisk > 0) return setPendingSwitch(activeId);
    followed.current = activeId;
    setPendingSwitch(null);
    void store.open(activeId);
  }, [activeId, store, atRisk]);

  // the drawers close on Escape; a panel opened on a narrow screen closes when the person picks something in it
  useEffect(() => {
    if (!navOpen && !membersOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      setNavOpen(false);
      setMembersOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [navOpen, membersOpen]);

  /** Before the user's own navigation replaces the open document: if the server has not received
   *  everything, ask (there is no offline storage: leaving loses those changes). */
  function leaveOpenDocument(): boolean {
    if (atRisk <= 0) return true;
    return window.confirm(
      "Your last changes are not on the server yet. If you leave this document they will not be saved to it. " +
        "A copy of the unsent text is kept on this device and shown in this room, where you can copy it or put it back. Leave this document?",
    );
  }

  const titles = new Map(docs.list.items.map((d) => [d.id, d.title] as const));
  /** the recovery copy may be put back only into the document that is open, and only by someone who can edit it */
  const canInsert = (r: RecoveryRecord) => r.documentId === docs.open?.id && liveRef.current?.docId === r.documentId && !!liveRef.current.canInsert();
  const insertRecovered = (r: RecoveryRecord) => {
    const live = liveRef.current;
    if (!live || live.docId !== r.documentId || !live.canInsert()) return false;
    return r.blocks.map((b) => live.insertText(b.inserted, b.blockId)).every(Boolean);
  };

  function openGuarded(id: string) {
    setNavOpen(false);
    setTab("document");
    if (store.openId === id) return;
    if (leaveOpenDocument()) {
      followed.current = activeId === id ? id : followed.current;
      void store.open(id);
    }
  }

  function switchNow() {
    const target = pendingSwitch;
    if (!target) return;
    followed.current = target;
    setPendingSwitch(null);
    void store.open(target);
  }

  if (state.status === "terminal" && state.terminalReason === "access_lost") {
    // read the text BEFORE this screen replaces the editor (which destroys the Yjs document)
    if (rescued.current === null && atRisk > 0) rescued.current = liveRef.current?.text() ?? "";
    const unsaved = rescued.current;
    return (
      <main className="centered">
        <h1>You no longer have access to this room</h1>
        {unsaved !== null && (
          <section className="conflict" role="alert" data-testid="access-lost-unsaved">
            <p>Your last changes had not reached the server. The text is still on this page; copy it before you leave.</p>
            <button
              onClick={() =>
                navigator.clipboard.writeText(unsaved).then(
                  () => setRecovered("Copied to the clipboard."),
                  () => setRecovered(unsaved),
                )
              }
            >
              Copy my unsaved text
            </button>
            {recovered && <textarea readOnly rows={8} value={recovered === "Copied to the clipboard." ? "" : recovered} placeholder={recovered} aria-label="Your unsaved text" />}
          </section>
        )}
        {/* the text this device kept (read-only here: access to the document is gone) */}
        <RecoveryPanel userId={me.id} roomId={roomId} titles={new Map()} canInsert={() => false} onInsert={() => false} />
        <button onClick={props.onBack}>Back to your rooms</button>
      </main>
    );
  }
  if (state.status === "terminal" && state.terminalReason === "sign_in_required") {
    return (
      <main className="centered">
        <h1>Your session ended</h1>
        <a href="/auth/login">Sign in again</a>
      </main>
    );
  }

  async function makeActive(id: string) {
    setNotice(null);
    try {
      await api.send("PUT", `/rooms/${roomId}/active-document`, {
        document_id: id,
        expected_selection_version: state.room?.active_document_version,
      });
      setDialog(null);
    } catch (err) {
      setNotice(
        err instanceof ApiError && err.code === "selection_conflict"
          ? "The active document changed while you were choosing. Look at the list again."
          : err instanceof ApiError
            ? err.message
            : "Could not reach the server.",
      );
    }
  }

  function created(doc: DocumentFull) {
    setDialog(null);
    void store.loadList();
    // The new document is already the room's active one. If the server has not received this tab's
    // last changes and the user keeps them, the normal "the room switched" banner takes over.
    if (!leaveOpenDocument()) return;
    followed.current = doc.id;
    setPendingSwitch(null);
    void store.open(doc.id);
  }

  const open = docs.open;
  const openDoc = open?.doc ?? null;
  const roleCanEdit = role === "owner" || role === "editor";
  const roomName = state.room?.name ?? "Loading…";

  function startResize(event: React.PointerEvent) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = chatWidth;
    const move = (e: PointerEvent) => setChatWidth(startWidth + (startX - e.clientX));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  const roomMenu = [
    { id: "invite", label: "Invite people", icon: <IconUserPlus />, hidden: !isOwner, onSelect: () => setDialog("invite") },
    { id: "members", label: isOwner ? "Members and invitations" : "Members", icon: <IconUsers />, onSelect: () => setMembersOpen(true) },
    { id: "start", label: "Start document", icon: <IconPlus />, hidden: !isOwner, separatorBefore: true, onSelect: () => setDialog("start") },
    { id: "open", label: "Open document…", icon: <IconDoc />, onSelect: () => setDialog("open") },
    { id: "ai", label: "AI settings", icon: <IconSettings />, separatorBefore: true, onSelect: () => setAiDialog(true) },
  ];

  const layoutStyle = { ["--chat-w" as string]: `${chatHidden ? 0 : chatWidth}px` } as React.CSSProperties;
  const docRow = (doc: DocumentSummary) => (
    <li key={doc.id} className={doc.id === open?.id ? "selected" : ""}>
      <button type="button" className="row-main" aria-current={doc.id === open?.id ? "true" : undefined} title={doc.title} onClick={() => void openGuarded(doc.id)}>
        <IconDoc size={16} />
        <span className="row-title">{doc.title}</span>
        {doc.is_active && <span className="dot-badge" title="The room's active document" aria-label="Active document" />}
      </button>
      <Menu
        label={`Actions for ${doc.title}`}
        align="end"
        triggerClassName="icon-button row-menu"
        trigger={<IconMore size={16} />}
        items={[
          { id: "open", label: "Open", onSelect: () => void openGuarded(doc.id) },
          { id: "active", label: "Make active for everyone", hidden: !isOwner || doc.is_active, onSelect: () => void makeActive(doc.id) },
        ]}
      />
    </li>
  );

  return (
    <div
      className="app-shell room-shell"
      style={layoutStyle}
      data-nav={navOpen ? "open" : "closed"}
      data-tab={tab}
      data-chat={chatHidden ? "hidden" : "shown"}
      data-members={membersOpen ? "open" : "closed"}
    >
      <div className="nav-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />
      <RoomRail rooms={rooms.rooms} currentId={roomId} onHome={props.onBack} onCreate={() => setDialog("create")} onJoin={() => setDialog("join")} />
      <nav className="sidebar" aria-label="Room navigation">
        <div className="sidebar-head">
          <h1 className="room-title">
            <Menu label={roomName} align="start" triggerClassName="room-menu-button" trigger={<><span className="room-name">{roomName}</span><IconChevronDown size={16} /></>} items={roomMenu} />
          </h1>
        </div>
        {isOwner && (
          <div className="sidebar-cta">
            <button type="button" className="invite-button" onClick={() => setDialog("invite")} data-testid="invite-people">
              <IconUserPlus size={16} /> Invite people
            </button>
          </div>
        )}
        <div className="sidebar-scroll">
          <section className="side-section" aria-label="Documents">
            <header className="side-heading">
              <h2 className="category">Documents</h2>
              {isOwner && (
                <Tip label="Start document" side="bottom">
                  <button type="button" className="icon-button" aria-label="Start document" onClick={() => setDialog("start")}>
                    <IconPlus size={16} />
                  </button>
                </Tip>
              )}
            </header>
            <ul className="doc-nav" aria-label="Documents in this room">
              {docs.list.items.map(docRow)}
            </ul>
            {docs.list.items.length === 0 && <p className="muted small side-empty">No documents yet.{isOwner ? " Start one with +." : ""}</p>}
            <button type="button" className="link side-link" onClick={() => setDialog("open")}>
              Open document…
            </button>
          </section>
          <SourcePanel store={ai} snap={aiSnap} role={role} documentId={docs.open?.id ?? null} userId={me.id} />
        </div>
        <UserPanel api={api} me={me} detail={role ? (role === "owner" ? "Owner" : role === "editor" ? "Editor" : "Viewer") : "…"} />
      </nav>

      <div className="main-column">
        <div className="top-bar">
          <button type="button" className="icon-button nav-toggle" aria-label="Open navigation" aria-expanded={navOpen} onClick={() => setNavOpen(!navOpen)}>
            <IconMenu />
          </button>
          <span className="top-title" title={roomName}>
            {roomName}
          </span>
          <span className="spacer" />
          <Tip label="Members" side="bottom">
            <button type="button" className="icon-button" aria-label="Show members" aria-pressed={membersOpen} onClick={() => setMembersOpen(!membersOpen)} data-testid="toggle-members">
              <IconUsers /> <span className="count">{state.members.length}</span>
            </button>
          </Tip>
          <Tip label={chatHidden ? "Show chat" : "Hide chat"} side="bottom">
            <button type="button" className="icon-button chat-toggle" aria-label={chatHidden ? "Show chat" : "Hide chat"} aria-pressed={!chatHidden} onClick={() => setChatHidden(!chatHidden)}>
              <IconChat />
            </button>
          </Tip>
        </div>
        <div className="tabs narrow-tabs" role="tablist" aria-label="Document or chat">
          <button type="button" role="tab" aria-selected={tab === "document"} className={tab === "document" ? "active" : ""} onClick={() => setTab("document")}>
            Document
          </button>
          <button type="button" role="tab" aria-selected={tab === "chat"} className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>
            Chat
          </button>
        </div>
      <main className="document-area" aria-label="Document">
        {notice && (
          <p role="alert" className="error notice-bar">
            {notice}
          </p>
        )}
        <RecoveryPanel userId={me.id} roomId={roomId} titles={titles} canInsert={canInsert} onInsert={insertRecovered} />
        {!open && (
          <section className="empty">
            <h2>{docs.list.items.length === 0 ? "No document yet" : "No document open"}</h2>
            {isOwner ? (
              <>
                <p>Start a new document, or open a saved one.</p>
                <div className="actions center">
                  <button className="primary" onClick={() => setDialog("start")}>
                    Start document
                  </button>
                  <button onClick={() => setDialog("open")}>Open document</button>
                </div>
              </>
            ) : (
              <p>The room owner has not chosen a document yet. It will appear here when they do.</p>
            )}
          </section>
        )}
        {open && (
          <>
            {open.loading && !openDoc && <p role="status">Loading document…</p>}
            {open.notFound && <p role="alert">This document is not available.</p>}
            {open.error && (
              <p role="alert" className="error">
                {open.error}
              </p>
            )}
            {openDoc && (
              <>
                <header className="doc-header">
                  <Title api={api} doc={openDoc} isOwner={isOwner} onRenamed={() => void store.open(openDoc.id)} />
                  <div className="doc-meta">
                    <span ref={setStatusSlot} className="status-slot" />
                    {openDoc.is_active ? <span className="chip-badge">Active document</span> : <span className="chip-badge muted-badge">Not the active document</span>}
                    {isOwner && !openDoc.is_active && (
                      <button type="button" className="compact" onClick={() => void makeActive(openDoc.id)}>
                        Make active
                      </button>
                    )}
                    <span className="chip-badge access" data-testid="access-mode">
                      {openDoc.can_edit && roleCanEdit ? "Access: can edit" : "Access: read-only"}
                    </span>
                  </div>
                </header>
                {pendingSwitch && (
                  <div className="conflict" role="alert" data-testid="switch-banner">
                    <strong>The room switched to a different document.</strong>
                    <p>Your last changes are not synced yet, so this document stays open until you choose.</p>
                    <div className="actions">
                      <button type="button" onClick={() => ((stayed.current = pendingSwitch), setPendingSwitch(null))} data-testid="stay">
                        Stay here
                      </button>
                      <button type="button" onClick={switchNow} data-testid="switch-anyway">
                        Switch anyway (a copy of those changes is kept here)
                      </button>
                    </div>
                  </div>
                )}
                <DocumentEditor
                  key={openDoc.id}
                  api={api}
                  roomId={roomId}
                  userId={me.id}
                  doc={openDoc}
                  members={state.members}
                  mobile={mobile}
                  roleCanEdit={roleCanEdit}
                  statusSlot={statusSlot}
                  onLive={(handle) => (liveRef.current = handle)}
                  onAtRisk={setAtRisk}
                />
              </>
            )}
          </>
        )}
      </main>
      </div>

      <div className="chat-column">
        <div
          className="chat-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the chat"
          aria-valuemin={CHAT_MIN}
          aria-valuemax={CHAT_MAX}
          aria-valuenow={chatWidth}
          tabIndex={0}
          onPointerDown={startResize}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft") setChatWidth(chatWidth + 20);
            if (e.key === "ArrowRight") setChatWidth(chatWidth - 20);
          }}
        />
        <ChatPanel
          client={client}
          userId={me.id}
          onHide={() => setChatHidden(true)}
          ai={{
            api,
            store: ai,
            snap: aiSnap,
            documentId: docs.open?.id ?? null,
            documentTitle: docs.open?.doc?.title ?? null,
            unsynced: atRisk,
            readSelection: async () => (await liveRef.current?.selection()) ?? null,
            readRange: async () => (await liveRef.current?.range()) ?? null,
            openDocumentPeers: () => (docs.open?.id && liveRef.current?.docId === docs.open.id ? liveRef.current.peers() : null),
            isOwner,
          }}
        />
        {membersOpen && (
          <MembersPanel
            api={api}
            roomId={roomId}
            meId={me.id}
            role={role}
            members={state.members}
            online={online}
            refreshKey={invitesTick}
            onInvite={() => setDialog("invite")}
            onClose={() => setMembersOpen(false)}
          />
        )}
      </div>
      {aiDialog && <AiSettingsDialog store={ai} snap={aiSnap} isOwner={isOwner} onClose={() => setAiDialog(false)} />}

      {dialog === "start" && <StartDialog api={api} roomId={roomId} onCreated={created} onClose={() => setDialog(null)} />}
      {dialog === "invite" && <InviteDialog api={api} roomId={roomId} roomName={roomName} onCreated={() => setInvitesTick((n) => n + 1)} onClose={() => setDialog(null)} />}
      {dialog === "create" && (
        <CreateRoomDialog
          api={api}
          onCreated={(room) => {
            setDialog(null);
            rooms.reload();
            window.location.hash = `#/rooms/${room.id}`;
          }}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog === "join" && <JoinRoomDialog onClose={() => setDialog(null)} />}
      {dialog === "open" && (
        <OpenDialog
          store={store}
          isOwner={isOwner}
          capabilities={capabilities}
          onOpen={(id) => {
            setDialog(null);
            void openGuarded(id);
          }}
          onMakeActive={makeActive}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  );
}

function Title(props: { api: Api; doc: DocumentFull; isOwner: boolean; onRenamed: () => void }) {
  const { api, doc } = props;
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(doc.title);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setValue(doc.title), [doc.title]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const title = value.trim();
    if (!title || title === doc.title) return setEditing(false);
    try {
      await api.send("PATCH", `/rooms/${doc.room_id}/documents/${doc.id}`, { title });
      setEditing(false);
      setError(null);
      props.onRenamed();
    } catch (err) {
      setError(err instanceof ApiError ? "That title is not valid." : "Could not reach the server.");
    }
  }

  if (!editing) {
    return (
      <div className="title-row">
        <h2 title={doc.title}>{doc.title}</h2>
        {props.isOwner && (
          <button type="button" className="icon-button" aria-label="Rename" title="Rename the document" onClick={() => setEditing(true)}>
            <IconEdit size={16} />
          </button>
        )}
      </div>
    );
  }
  return (
    <form className="title-row" onSubmit={save}>
      <label className="sr-only" htmlFor="doc-title">
        Document title
      </label>
      <input id="doc-title" value={value} onChange={(e) => setValue(e.target.value)} maxLength={400} autoFocus />
      <button type="submit" className="primary compact">
        Save title
      </button>
      <button type="button" className="compact" onClick={() => setEditing(false)}>
        Cancel
      </button>
      {error && (
        <span role="alert" className="error">
          {error}
        </span>
      )}
    </form>
  );
}
