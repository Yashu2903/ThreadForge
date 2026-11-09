/**
 * The chat composer with the `@AI` mention.
 *
 * `@AI` is an EXPLICIT ENTITY: the person chooses it from a menu (typing "@" at the start of a word
 * opens it, or the "Ask AI" button), and it becomes a removable chip. Text that merely contains
 * "@AI" never triggers anything: it is sent as an ordinary message. Pasted text, quoted messages and
 * AI replies never turn the chip on; only the person's own choice does.
 *
 * With the chip on, the composer shows exactly what the request will use, as removable chips:
 *   - the chosen files (each with its analysis state; an unanalyzed file says it "will be analyzed
 *     when you send" and how big that is),
 *   - "Use open document" (with the real title; disabled while the editor reports unsynced changes),
 *   - "Use selected blocks" (the blocks highlighted in the editor right now),
 *   - "Include chat context" (off by default; messages are picked explicitly in the chat list).
 * Nothing persists silently from one request to the next: after a send the chips reset, and a
 * remembered selection (last files) is shown, labelled, and clearable.
 * Output is "Answer in chat", the only output that exists in this phase; no other control is shown.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api";
import type { BlockSelection } from "../DocumentEditor";
import { type AiSnapshot, type AiStore, type SendBody, unavailableReason } from "./store";
import { ERROR_TEXT, type EstimateReply, type SourceItem } from "./types";

/** the estimate the server returns inside 409 confirmation_required */
interface ConfirmInfo {
  input_tokens: number;
  calls: number;
  cost_known: boolean;
  expected_cost_micros: number | null;
}

type Hint = SendBody["task_hint"];
const TASKS: { value: Hint; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "ask", label: "Ask" },
  { value: "summarize", label: "Summarize" },
  { value: "compare", label: "Compare" },
];

export interface ComposerContext {
  documentId: string | null;
  documentTitle: string | null;
  /** unsynced local changes in the open editor (0 = everything is on the server) */
  unsynced: number;
  readSelection: () => Promise<BlockSelection | null>;
  /** chat messages the person ticked in the list */
  chatSelection: string[];
  clearChatSelection: () => void;
  role: string | null;
}

export function Composer(props: {
  store: AiStore;
  snap: AiSnapshot;
  ctx: ComposerContext;
  sendPlain: (text: string) => Promise<boolean>;
  onTyping: () => void;
}) {
  const { store, snap, ctx } = props;
  const [text, setText] = useState("");
  const [ai, setAi] = useState(false);
  const [menu, setMenu] = useState(false);
  const [task, setTask] = useState<Hint>("auto");
  const [picked, setPicked] = useState<string[]>([]); // source VERSION ids
  const [useDoc, setUseDoc] = useState(false);
  const [useBlocks, setUseBlocks] = useState(false);
  const [useChat, setUseChat] = useState(false);
  const [selection, setSelection] = useState<BlockSelection | null>(null);
  const [estimate, setEstimate] = useState<EstimateReply | null>(null);
  const [confirm, setConfirm] = useState<ConfirmInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const requestId = useRef(crypto.randomUUID());

  const blocked = unavailableReason(snap, ctx.role);
  const viewer = ctx.role === "viewer";
  const sources = useMemo(
    () => (snap.sources?.sources ?? []).filter((s): s is SourceItem & { current_version: NonNullable<SourceItem["current_version"]> } => s.current_version !== null),
    [snap.sources],
  );
  const chosen = sources.filter((s) => picked.includes(s.current_version.id));
  const unsyncedBlock = ctx.unsynced > 0;

  // the editor's selection can change while the chip is on: re-read it when the option is on
  useEffect(() => {
    if (!ai || !useBlocks) return setSelection(null);
    let alive = true;
    const read = () => void ctx.readSelection().then((s) => alive && setSelection(s));
    read();
    const t = setInterval(read, 1000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [ai, useBlocks, ctx]);

  // keep a picked file only while it still exists (a removed file disappears from the chips)
  useEffect(() => {
    setPicked((p) => p.filter((id) => sources.some((s) => s.current_version.id === id)));
  }, [sources]);

  function body(confirmLarge: boolean): SendBody | null {
    if (!ctx.documentId) return null;
    const scope = useBlocks ? "blocks" : useDoc ? "whole" : "none";
    return {
      client_request_id: requestId.current,
      instruction: text.replace(/^@AI\s*/i, "").trim(),
      task_hint: task,
      document_id: ctx.documentId,
      include_document_content: scope !== "none",
      document_scope: scope,
      selected_block_ids: scope === "blocks" && selection ? selection.ids : [],
      selection_hash: scope === "blocks" && selection ? selection.hash : null,
      source_version_ids: picked,
      include_chat_context: useChat && ctx.chatSelection.length > 0,
      chat_message_ids: useChat ? ctx.chatSelection : [],
      confirm_large: confirmLarge,
    };
  }

  // the estimate follows the choices (debounced); it never blocks typing
  useEffect(() => {
    if (!ai || !ctx.documentId || !text.trim()) return setEstimate(null);
    const b = body(false);
    if (!b) return;
    let alive = true;
    const t = setTimeout(() => {
      void store
        .estimate(b, selection?.chars ?? 0)
        .then((e) => alive && setEstimate(e))
        .catch(() => alive && setEstimate(null));
    }, 500);
    return () => {
      alive = false;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ai, text, task, picked, useDoc, useBlocks, useChat, ctx.chatSelection.join(","), selection?.hash]);

  function onChange(value: string) {
    setText(value);
    props.onTyping();
    // "@" at the start of a word offers the mention; it only OFFERS it
    const last = value.slice(-3);
    setMenu(!ai && !viewer && /(^|\s)@\w{0,2}$/.test(last) && value.endsWith("@") === true);
  }

  function chooseAi() {
    setAi(true);
    setMenu(false);
    setText((t) => t.replace(/(^|\s)@\w{0,2}$/, "$1"));
    requestId.current = crypto.randomUUID();
  }

  function reset() {
    setText("");
    setAi(false);
    setPicked([]);
    setUseDoc(false);
    setUseBlocks(false);
    setUseChat(false);
    setEstimate(null);
    setConfirm(null);
    ctx.clearChatSelection();
    requestId.current = crypto.randomUUID();
  }

  async function sendAi(confirmLarge: boolean) {
    const b = body(confirmLarge);
    if (!b || !b.instruction) return;
    setBusy(true);
    setError(null);
    try {
      await store.send(b);
      reset();
    } catch (err) {
      if (err instanceof ApiError && err.code === "confirmation_required") {
        setConfirm((err.extra.estimate as ConfirmInfo) ?? null);
      } else if (err instanceof ApiError && err.code === "consent_required") {
        setError("Accept the AI data notice first (AI settings).");
      } else if (err instanceof ApiError) {
        setError(ERROR_TEXT[err.code] ?? err.message);
      } else {
        setError("Could not reach the server.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (ai) return sendAi(false);
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      if (await props.sendPlain(text)) setText("");
      else setError("Your message was not confirmed yet. It will be retried; do not send it again.");
    } finally {
      setBusy(false);
    }
  }

  const disabledAi = Boolean(blocked) || !ctx.documentId;
  return (
    <form className="composer" onSubmit={submit} aria-label="Message composer">
      {ai && (
        <div className="chips" aria-label="What this request will use">
          <span className="chip mention" data-testid="ai-chip">
            @AI
            <button type="button" aria-label="Remove @AI" onClick={() => setAi(false)}>
              ×
            </button>
          </span>
          {chosen.map((s) => (
            <span className="chip" key={s.id}>
              {s.display_name} · {stateLabel(s.current_version.analysis_state)}
              {s.current_version.analysis_state !== "ready" && (
                <small>
                  {" "}
                  will be analyzed when you send ({sizeNote(s.current_version.page_count, s.current_version.byte_size)})
                </small>
              )}
              <button type="button" aria-label={`Remove ${s.display_name}`} onClick={() => setPicked(picked.filter((i) => i !== s.current_version.id))}>
                ×
              </button>
            </span>
          ))}
          {(useDoc || useBlocks) && (
            <span className="chip">
              {useBlocks ? "Selected blocks" : "Open document"}: {ctx.documentTitle ?? "document"}
              <button type="button" aria-label="Remove document" onClick={() => (setUseDoc(false), setUseBlocks(false))}>
                ×
              </button>
            </span>
          )}
          {useChat && (
            <span className="chip">
              Chat context: {ctx.chatSelection.length} message(s)
              <button type="button" aria-label="Remove chat context" onClick={() => setUseChat(false)}>
                ×
              </button>
            </span>
          )}
        </div>
      )}

      {ai && (
        <fieldset className="ai-options">
          <legend className="sr-only">AI request options</legend>
          <div className="tasks" role="group" aria-label="Task">
            {TASKS.map((t) => (
              <button key={t.value} type="button" aria-pressed={task === t.value} className={task === t.value ? "primary" : ""} onClick={() => setTask(t.value)}>
                {t.label}
              </button>
            ))}
          </div>
          <details>
            <summary>Files ({picked.length} chosen)</summary>
            {sources.length === 0 && <p className="muted">No files in this room yet. Upload one in the Files panel.</p>}
            {picked.length === 0 && sources.length > 0 && <p className="muted">Nothing is chosen. Pick the files this request may use.</p>}
            <ul className="source-pick">
              {sources.map((s) => (
                <li key={s.id}>
                  <label>
                    <input
                      type="checkbox"
                      checked={picked.includes(s.current_version.id)}
                      disabled={picked.length >= 10 && !picked.includes(s.current_version.id)}
                      onChange={(e) =>
                        setPicked(e.target.checked ? [...picked, s.current_version.id] : picked.filter((i) => i !== s.current_version.id))
                      }
                    />{" "}
                    {s.display_name} <span className="muted">({stateLabel(s.current_version.analysis_state)})</span>
                  </label>
                </li>
              ))}
            </ul>
          </details>
          <label className={unsyncedBlock ? "muted" : ""}>
            <input
              type="checkbox"
              checked={useDoc}
              disabled={unsyncedBlock || !ctx.documentId}
              onChange={(e) => (setUseDoc(e.target.checked), e.target.checked && setUseBlocks(false))}
            />{" "}
            Use open document{ctx.documentTitle ? ` “${ctx.documentTitle}”` : ""}
          </label>
          <label className={unsyncedBlock ? "muted" : ""}>
            <input
              type="checkbox"
              checked={useBlocks}
              disabled={unsyncedBlock || !ctx.documentId}
              onChange={(e) => (setUseBlocks(e.target.checked), e.target.checked && setUseDoc(false))}
            />{" "}
            Use selected blocks{useBlocks ? (selection ? ` (${selection.ids.length} selected)` : " (select text in the document first)") : ""}
          </label>
          {unsyncedBlock && (
            <p className="muted" role="status">
              Your latest changes are not on the server yet, so the document cannot be included. Wait for “Saved”.
            </p>
          )}
          <label>
            <input type="checkbox" checked={useChat} onChange={(e) => setUseChat(e.target.checked)} /> Include chat context
            {useChat && ` (${ctx.chatSelection.length} message(s) ticked above)`}
          </label>
          <p className="muted small">Output: Answer in chat.</p>
          {estimate && <EstimateLine estimate={estimate} />}
          {snap.consent && !snap.consent.accepted && (
            <p role="status" className="muted">
              Before your first request you need to accept the AI data notice (AI settings).
            </p>
          )}
        </fieldset>
      )}

      {menu && !ai && (
        <ul className="mention-menu" role="listbox" aria-label="Mention">
          <li>
            <button type="button" role="option" aria-selected={true} onClick={chooseAi} disabled={disabledAi}>
              @AI — ask the AI about this room's files
            </button>
          </li>
        </ul>
      )}

      <label className="sr-only" htmlFor="chat-input">
        Message
      </label>
      <textarea
        id="chat-input"
        value={text}
        rows={2}
        maxLength={ai ? 4000 : 5000}
        placeholder={ai ? "Ask the AI…" : "Message"}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) void submit(e as unknown as React.FormEvent);
          if (e.key === "Escape") setMenu(false);
        }}
      />
      {blocked && !viewer && (
        <p className="muted" role="status">
          {blocked}
        </p>
      )}
      {viewer && (
        <p className="muted" role="status">
          AI requests are for editors and owners. You can still read the answers.
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {confirm && (
        <div role="alertdialog" aria-label="Confirm a large request" className="confirm">
          <p>
            This request is large: about {confirm.input_tokens.toLocaleString("en-US")} input tokens and {confirm.calls} calls
            {confirm.cost_known && confirm.expected_cost_micros !== null ? `, roughly $${(confirm.expected_cost_micros / 1e6).toFixed(4)}` : ""}.
          </p>
          <button type="button" className="primary" onClick={() => void sendAi(true)}>
            Send anyway
          </button>
          <button type="button" onClick={() => setConfirm(null)}>
            Change it
          </button>
        </div>
      )}
      <div className="composer-actions">
        {!ai && (
          <button type="button" disabled={disabledAi || viewer} title={blocked ?? "Ask the AI"} onClick={chooseAi}>
            Ask AI
          </button>
        )}
        <button type="submit" className="primary" disabled={busy || !text.trim() || (ai && (disabledAi || (useBlocks && !selection)))}>
          {ai ? "Send to AI" : "Send"}
        </button>
      </div>
    </form>
  );
}

function stateLabel(state: string): string {
  return state === "not_analyzed" ? "not analyzed yet" : state.replace("_", " ");
}

function sizeNote(pages: number | null, bytes: number): string {
  return pages ? `${pages} page${pages === 1 ? "" : "s"}` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function EstimateLine({ estimate }: { estimate: EstimateReply }) {
  const e = estimate.estimate;
  const who = Object.values(estimate.key_sources).every((k) => k.source === "user") ? "your key" : Object.values(estimate.key_sources).some((k) => k.source === "user") ? "your key and the server's" : "the server's key";
  return (
    <p className="muted small" data-testid="estimate">
      About {e.input_tokens.toLocaleString("en-US")} tokens in, up to {e.provider_calls} calls
      {e.cost_known && e.expected_cost_micros !== null ? `, roughly $${(e.expected_cost_micros / 1e6).toFixed(4)}` : ""}; charged to {who}.
      {e.new_analysis_pages > 0 ? ` Analyzes ${e.new_analysis_pages} new page(s).` : ""}
      {estimate.blocking_code ? ` Blocked: ${ERROR_TEXT[estimate.blocking_code] ?? estimate.blocking_code}.` : ""}
    </p>
  );
}
