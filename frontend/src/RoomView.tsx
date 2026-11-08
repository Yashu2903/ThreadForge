import { useEffect, useRef, useState } from "react";
import { type Api, ApiError } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { DocumentRead } from "./components/DocumentRead";
import { OpenDialog } from "./components/OpenDialog";
import { StartDialog } from "./components/StartDialog";
import type { Capabilities, DocumentFull, Me } from "./types";
import { DocumentEditor } from "./DocumentEditor";
import type { DocumentSession, ReadOnlyReason } from "./documents/session";
import { fontNotice } from "./editor/fonts";
import { useClientState, useRoomHandles, useStore } from "./useRoom";

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
 * the open document. A clean client follows the room's active document when the owner changes it.
 */
export function RoomView(props: { api: Api; me: Me; roomId: string; onBack: () => void }) {
  const { api, me, roomId } = props;
  const { client, store } = useRoomHandles(api, roomId, me.id);
  const state = useClientState(client);
  const docs = useStore(store);
  const [dialog, setDialog] = useState<"start" | "open" | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const sessionRef = useRef<DocumentSession | null>(null);
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null);
  const [switchError, setSwitchError] = useState<string | null>(null);
  const [recovered, setRecovered] = useState<string | null>(null);
  const editedDocs = useRef(new Set<string>());
  const mobile = useIsMobile();

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

  // Follow the room's active document. A CLEAN client opens the new one; a client with unsaved work
  // keeps its document, says the room switched, and lets the user choose (never replaced silently).
  const followed = useRef<string | null>(null);
  useEffect(() => {
    if (!activeId) return;
    if (followed.current === activeId) return setPendingSwitch(null);
    const session = sessionRef.current;
    if (session && session.docId !== activeId && session.hasUnsavedWork) return setPendingSwitch(activeId);
    followed.current = activeId;
    setPendingSwitch(null);
    void store.open(activeId);
  }, [activeId, store]);

  /** Before the user's own navigation replaces the open document: save first; if that fails, ask. */
  async function leaveOpenDocument(): Promise<boolean> {
    const session = sessionRef.current;
    if (!session || !session.hasUnsavedWork) return true;
    await session.saveNow();
    if (!session.hasUnsavedWork) return true;
    return window.confirm("This document has changes that could not be saved. Leave it and discard them?");
  }

  async function openGuarded(id: string) {
    if (store.openId === id) return;
    if (await leaveOpenDocument()) {
      followed.current = activeId === id ? id : followed.current;
      void store.open(id);
    }
  }

  async function switchNow(save: boolean) {
    const target = pendingSwitch;
    if (!target) return;
    setSwitchError(null);
    const session = sessionRef.current;
    if (save && session) {
      await session.saveNow();
      if (session.hasUnsavedWork) return setSwitchError("Your changes could not be saved, so the document was not switched. Resolve the problem above, or discard.");
    }
    followed.current = target;
    setPendingSwitch(null);
    void store.open(target);
  }

  if (state.status === "terminal" && state.terminalReason === "access_lost") {
    const unsaved = sessionRef.current?.hasUnsavedWork ? sessionRef.current.localText() : null;
    return (
      <main className="centered">
        <h1>You no longer have access to this room</h1>
        {unsaved !== null && (
          <section className="conflict" role="alert" data-testid="access-lost-unsaved">
            <p>You had changes that were not saved. They are still on this page; copy them before you leave.</p>
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

  async function created(doc: DocumentFull) {
    setDialog(null);
    void store.loadList();
    // The new document is already the room's active one. If this tab holds unsaved work that cannot
    // be saved and the user keeps it, the normal "the room switched" banner takes over.
    if (!(await leaveOpenDocument())) return;
    followed.current = doc.id;
    setPendingSwitch(null);
    void store.open(doc.id);
  }

  const open = docs.open;
  const openDoc = open?.doc ?? null;
  const roleCanEdit = role === "owner" || role === "editor";
  // Once an editor is on screen for a document it STAYS (read-only if rights are lost), so unsaved
  // text is never torn away by a role change.
  if (openDoc && roleCanEdit && openDoc.can_edit && !mobile) editedDocs.current.add(openDoc.id);
  const showEditor = !!openDoc && editedDocs.current.has(openDoc.id);
  const readOnlyReason: ReadOnlyReason | null = roleCanEdit && openDoc?.can_edit ? (mobile ? "mobile" : null) : "role_changed";

  return (
    <div className="room-layout">
      <nav className="sidebar" aria-label="Room navigation">
        <button className="link" onClick={props.onBack}>
          ← Rooms
        </button>
        <h1 title={state.room?.name}>{state.room?.name ?? "Loading…"}</h1>
        <p className="muted">
          {me.display_name} · {role ?? "…"}
        </p>
        <h2>Documents</h2>
        {isOwner && (
          <div className="stack">
            <button className="primary" onClick={() => setDialog("start")}>
              Start document
            </button>
          </div>
        )}
        <button onClick={() => setDialog("open")}>Open document</button>
        <ul className="doc-nav" aria-label="Documents in this room">
          {docs.list.items.map((doc) => (
            <li key={doc.id}>
              <button
                className={doc.id === open?.id ? "selected" : ""}
                aria-current={doc.id === open?.id ? "true" : undefined}
                onClick={() => void openGuarded(doc.id)}
              >
                {doc.title}
              </button>
              {doc.is_active && <span className="badge">Active</span>}
            </li>
          ))}
        </ul>
      </nav>

      <main className="document-area" aria-label="Document">
        {notice && (
          <p role="alert" className="error">
            {notice}
          </p>
        )}
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
                    <span data-testid="access-mode">{openDoc.can_edit ? "Access: can edit" : "Access: read-only"}</span>
                    <span className="muted" data-testid="header-version">
                      Version {openDoc.content_version}
                    </span>
                  </div>
                </header>
                {!showEditor && fontNotice(openDoc.content) && (
                  <p className="font-notice" role="note" data-testid="font-notice">
                    {fontNotice(openDoc.content)}
                  </p>
                )}
                {pendingSwitch && (
                  <div className="conflict" role="alert" data-testid="switch-banner">
                    <strong>The room switched to a different document.</strong>
                    <p>You have unsaved changes here, so this document stays open until you choose.</p>
                    <div className="actions">
                      <button type="button" onClick={() => void switchNow(true)} data-testid="save-then-switch">
                        Save, then switch
                      </button>
                      <button type="button" onClick={() => void switchNow(false)} data-testid="discard-switch">
                        Discard my changes and switch
                      </button>
                    </div>
                    {switchError && <p className="error">{switchError}</p>}
                  </div>
                )}
                {mobile && (
                  <p className="muted" role="note" data-testid="mobile-note">
                    Editing is not supported in mobile browsers; you can view this document.
                  </p>
                )}
                {showEditor && !mobile ? (
                  <DocumentEditor
                    key={openDoc.id}
                    api={api}
                    roomId={roomId}
                    doc={openDoc}
                    serverDoc={openDoc}
                    store={store}
                    readOnlyReason={readOnlyReason}
                    onSession={(s) => (sessionRef.current = s)}
                  />
                ) : (
                  <DocumentRead content={openDoc.content} roomId={roomId} />
                )}
              </>
            )}
          </>
        )}
      </main>

      <ChatPanel client={client} userId={me.id} />

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
