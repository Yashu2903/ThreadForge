import { useEffect, useRef, useState } from "react";
import type { RoomClient } from "threadforge-client";
import { useClientState } from "../useRoom";

/**
 * Minimal chat beside the document: the room client's messages, a composer, and "is typing". It
 * exists so we can verify chat keeps working next to the editor; it adds no chat features.
 */
export function ChatPanel(props: { client: RoomClient; userId: string }) {
  const state = useClientState(props.client);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const messages = [...state.messages.values()].sort(
    (a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id),
  );
  const names = new Map(state.members.map((m) => [m.user_id, m.display_name]));
  const typing = state.typing.userIds.filter((id) => id !== props.userId).map((id) => names.get(id) ?? "Someone");

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length]);

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const body = draft;
    if (!body.trim() || sending) return;
    setSending(true);
    setError(null);
    try {
      const sent = await props.client.sendMessage(body);
      if (sent) setDraft("");
      else setError("Your message was not confirmed yet. It will be retried; do not send it again.");
    } finally {
      setSending(false);
    }
  }

  return (
    <aside className="chat" aria-label="Chat">
      <h2>Chat</h2>
      <p className="muted" role="status">
        {state.status === "live" ? "Live" : state.status === "terminal" ? "Disconnected" : "Connecting…"}
      </p>
      <div className="messages" role="log" aria-live="polite" aria-label="Messages">
        {messages.map((m) => (
          <div key={m.id} className="message">
            <strong>{m.is_deleted ? "" : m.author_display_name}</strong>
            <span>{m.is_deleted ? "This message was deleted." : m.body}</span>
          </div>
        ))}
        <div ref={endRef} />
      </div>
      <p className="typing" aria-live="polite">
        {typing.length > 0 ? `${typing.join(", ")} ${typing.length === 1 ? "is" : "are"} typing…` : " "}
      </p>
      <form onSubmit={send}>
        <label className="sr-only" htmlFor="chat-input">
          Message
        </label>
        <textarea
          id="chat-input"
          value={draft}
          rows={2}
          maxLength={5000}
          onChange={(e) => {
            setDraft(e.target.value);
            props.client.userTyping();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) void send(e as unknown as React.FormEvent);
          }}
        />
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <button type="submit" className="primary" disabled={sending || !draft.trim()}>
          Send
        </button>
      </form>
    </aside>
  );
}
