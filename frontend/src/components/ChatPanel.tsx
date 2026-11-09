import { useEffect, useRef, useState } from "react";
import type { RoomClient } from "threadforge-client";
import type { Api } from "../api";
import { Composer } from "../ai/Composer";
import { AnswerCard, RequestCard } from "../ai/Cards";
import type { AiSnapshot, AiStore } from "../ai/store";
import type { BlockSelection } from "../DocumentEditor";
import { useClientState } from "../useRoom";

export interface ChatAi {
  api: Api;
  store: AiStore;
  snap: AiSnapshot;
  documentId: string | null;
  documentTitle: string | null;
  unsynced: number;
  readSelection: () => Promise<BlockSelection | null>;
}

/**
 * Chat beside the document: the room client's messages, the composer (with the `@AI` mention), and
 * "is typing". AI messages render as cards: an `@AI` message shows its progress to the person who asked
 * (and to owners/editors who can write the document); a published answer shows to EVERY member,
 * viewers included. Everything the model wrote is shown as text.
 */
export function ChatPanel(props: { client: RoomClient; userId: string; ai: ChatAi }) {
  const state = useClientState(props.client);
  const { ai } = props;
  const [chatSelection, setChatSelection] = useState<string[]>([]);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const messages = [...state.messages.values()].sort(
    (a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id),
  );
  const names = new Map(state.members.map((m) => [m.user_id, m.display_name]));
  const typing = state.typing.userIds.filter((id) => id !== props.userId).map((id) => names.get(id) ?? "Someone");
  const role = state.role;
  const canAsk = role === "owner" || role === "editor";

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length]);

  async function deleteAnswer(messageId: string, version: number) {
    setDeleteError(null);
    try {
      await ai.api.send("DELETE", `/rooms/${props.client.state.room?.id}/messages/${messageId}?expected_version=${version}`);
    } catch {
      setDeleteError("The answer could not be deleted.");
    }
  }

  return (
    <aside className="chat" aria-label="Chat">
      <h2>Chat</h2>
      <p className="muted" role="status">
        {state.status === "live" ? "Live" : state.status === "terminal" ? "Disconnected" : "Connecting…"}
      </p>
      <div className="messages" role="log" aria-live="polite" aria-label="Messages">
        {messages.map((m) => {
          const kind = m.kind ?? "user";
          const selectable = canAsk && kind === "user" && !m.is_deleted;
          return (
            <div key={m.id} className={`message ${kind}`} data-kind={kind}>
              <strong>{m.is_deleted ? "" : m.author_display_name}</strong>
              {kind === "ai_answer" && m.ai_request_id && !m.is_deleted ? (
                <AnswerMessage ai={ai} requestId={m.ai_request_id} fallback={m.body ?? ""} roomId={props.client.state.room?.id ?? ""} />
              ) : (
                <span>{m.is_deleted ? "This message was deleted." : m.body}</span>
              )}
              {kind === "ai_request" && m.ai_request_id && !m.is_deleted && <RequestMessage ai={ai} requestId={m.ai_request_id} />}
              {selectable && (
                <label className="small muted">
                  <input
                    type="checkbox"
                    checked={chatSelection.includes(m.id)}
                    disabled={!chatSelection.includes(m.id) && chatSelection.length >= 20}
                    onChange={(e) => setChatSelection(e.target.checked ? [...chatSelection, m.id] : chatSelection.filter((i) => i !== m.id))}
                  />{" "}
                  Add to AI context
                </label>
              )}
              {kind === "ai_answer" && role === "owner" && !m.is_deleted && (
                <button className="link small" onClick={() => void deleteAnswer(m.id, m.version)}>
                  Delete this answer
                </button>
              )}
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
      {deleteError && (
        <p role="alert" className="error">
          {deleteError}
        </p>
      )}
      <p className="typing" aria-live="polite">
        {typing.length > 0 ? `${typing.join(", ")} ${typing.length === 1 ? "is" : "are"} typing…` : " "}
      </p>
      <Composer
        store={ai.store}
        snap={ai.snap}
        ctx={{
          documentId: ai.documentId,
          documentTitle: ai.documentTitle,
          unsynced: ai.unsynced,
          readSelection: ai.readSelection,
          chatSelection,
          clearChatSelection: () => setChatSelection([]),
          role,
        }}
        sendPlain={async (text) => Boolean(await props.client.sendMessage(text))}
        onTyping={() => props.client.userTyping()}
      />
    </aside>
  );
}

function RequestMessage({ ai, requestId }: { ai: ChatAi; requestId: string }) {
  useEffect(() => {
    ai.store.watch(requestId);
  }, [ai.store, requestId]);
  const info = ai.store.requests.get(requestId);
  void ai.snap.version; // re-render when the store changes
  if (!info || typeof info === "string" || info.state === "succeeded") return null;
  return <RequestCard store={ai.store} info={info} />;
}

function AnswerMessage({ ai, requestId, fallback, roomId }: { ai: ChatAi; requestId: string; fallback: string; roomId: string }) {
  useEffect(() => {
    void ai.store.ensureResult(requestId);
  }, [ai.store, requestId]);
  void ai.snap.version;
  const result = ai.store.results.get(requestId);
  if (!result || result === "missing") return <span>{fallback}</span>;
  return <AnswerCard api={ai.api} roomId={roomId} result={result} requestId={requestId} />;
}
