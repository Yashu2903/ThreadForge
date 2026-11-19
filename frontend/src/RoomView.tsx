import { useEffect, useRef, useState } from "react";
import { type Api, ApiError } from "./api";
import { AiSettingsDialog, SourcePanel } from "./ai/Panels";
import { ChatPanel } from "./components/ChatPanel";
import { OpenDialog } from "./components/OpenDialog";
import { RecoveryPanel } from "./components/RecoveryPanel";
import { RoomRail, colourFor, initialOf, useRoomList } from "./components/RoomRail";
import type { RecoveryRecord } from "./collab/recovery";
import { StartDialog } from "./components/StartDialog";
import type { Capabilities, DocumentFull, Me } from "./types";
import { DocumentEditor, type LiveHandle } from "./DocumentEditor";
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

/**
 * The room: navigation (documents) on the left, the document area in the middle, chat on the right.
 * One RoomClient feeds both the chat and the document events; the DocumentStore holds the list and
 * the open document's METADATA; the DocumentEditor holds the live connection to the text. A client
 * with nothing unsynced follows the room's active document when the owner changes it.
 */
export function RoomView(props: { api: Api; me: Me; roomId: string; onBack: () => void }) {
  const { api, me, roomId } = props;
  const { client, store, ai } = useRoomHandles(api, roomId, me.id);
  const state = useClientState(client);
  const aiSnap = useAi(ai);
  const [aiDialog, setAiDialog] = useState(false);
  const docs = useStore(store);
  const [dialog, setDialog] = useState<"start" | "open" | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const liveRef = useRef<LiveHandle | null>(null);
  const rescued = useRef<string | null>(null);
  const [atRisk, setAtRisk] = useState(0);
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null);
  const [recovered, setRecovered] = useState<string | null>(null);
  const mobile = useIsMobile();
  const rooms = useRoomList(api);

  const role = state.role;
  const isOwner = role === "owner";
  const activeId = state.room?.active_document_id ?? null;

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

  return (
    <div className="room-layout">
      <RoomRail rooms={rooms} currentId={roomId} onHome={props.onBack} />
      <nav className="sidebar" aria-label="Room navigation">
        <h1 className="room-title" title={state.room?.name}>
          {state.room?.name ?? "Loading…"}
        </h1>
        <div className="sidebar-scroll">
          <h2 className="category">Documents</h2>
          <ul className="doc-nav" aria-label="Documents in this room">
            {docs.list.items.map((doc) => (
              <li key={doc.id}>
                <button
                  className={doc.id === open?.id ? "selected" : ""}
                  aria-current={doc.id === open?.id ? "true" : undefined}
                  onClick={() => void openGuarded(doc.id)}
                >
                  <span className="hash" aria-hidden="true">
                    #
                  </span>
                  {doc.title}
                </button>
                {doc.is_active && <span className="badge">Active</span>}
              </li>
            ))}
          </ul>
          <div className="sidebar-actions">
            {isOwner && (
              <button className="primary" onClick={() => setDialog("start")}>
                Start document
              </button>
            )}
            <button onClick={() => setDialog("open")}>Open document</button>
            <button onClick={() => setAiDialog(true)}>AI settings</button>
          </div>
          <SourcePanel store={ai} snap={aiSnap} role={role} documentId={docs.open?.id ?? null} userId={me.id} />
        </div>
        <div className="user-panel">
          <span className="avatar" style={{ background: colourFor(me.id) }} data-initial={initialOf(me.display_name)} aria-hidden="true" />
          <span className="user-text">
            <strong>{me.display_name}</strong>
            <small className="muted">{role ?? "…"}</small>
          </span>
        </div>
      </nav>

      <main className="document-area" aria-label="Document">
        {notice && (
          <p role="alert" className="error">
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
                <div className="actions">
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
                    {openDoc.is_active ? <span className="badge">Active document</span> : <span className="badge muted-badge">Not the active document</span>}
                    {isOwner && !openDoc.is_active && (
                      <button onClick={() => void makeActive(openDoc.id)}>Make active</button>
                    )}
                    <span data-testid="access-mode">{openDoc.can_edit && roleCanEdit ? "Access: can edit" : "Access: read-only"}</span>
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
                  onLive={(handle) => (liveRef.current = handle)}
                  onAtRisk={setAtRisk}
                />
              </>
            )}
          </>
        )}
      </main>

      <ChatPanel
        client={client}
        userId={me.id}
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
      <aside className="members" aria-label="Members">
        <h2 className="category">Members — {state.members.length}</h2>
        {(["owner", "editor", "viewer"] as const).map((r) => {
          const group = state.members.filter((m) => m.role === r);
          if (group.length === 0) return null;
          return (
            <div key={r} className="member-group">
              <h3 className="category">
                {r === "owner" ? "Owner" : r === "editor" ? "Editors" : "Viewers"} — {group.length}
              </h3>
              <ul>
                {group.map((m) => (
                  <li key={m.user_id}>
                    <span className="avatar" style={{ background: colourFor(m.user_id) }} data-initial={initialOf(m.display_name)} aria-hidden="true" />
                    <span className="member-name">{m.display_name}</span>
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </aside>
      {aiDialog && <AiSettingsDialog store={ai} snap={aiSnap} isOwner={isOwner} onClose={() => setAiDialog(false)} />}

      {dialog === "start" && <StartDialog api={api} roomId={roomId} onCreated={created} onClose={() => setDialog(null)} />}
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
        <h2>{doc.title}</h2>
        {props.isOwner && <button onClick={() => setEditing(true)}>Rename</button>}
      </div>
    );
  }
  return (
    <form className="title-row" onSubmit={save}>
      <label className="sr-only" htmlFor="doc-title">
        Document title
      </label>
      <input id="doc-title" value={value} onChange={(e) => setValue(e.target.value)} maxLength={400} autoFocus />
      <button type="submit" className="primary">
        Save title
      </button>
      <button type="button" onClick={() => setEditing(false)}>
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
