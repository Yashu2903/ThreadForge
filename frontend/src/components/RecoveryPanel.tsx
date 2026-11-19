import { useState, useSyncExternalStore } from "react";
import { type RecoveryReason, type RecoveryRecord, recoveryStore } from "../collab/recovery";

const WHY: Record<RecoveryReason, string> = {
  replaced_while_pending:
    "You typed this, but it had not reached the server when the shared document changed (an AI edit or another person's change replaced that text). The document now shows the newer text. Your version was kept here instead of being merged over it.",
  left_with_unsent_changes: "You left this document (or closed the page) before these changes reached the server.",
  access_lost: "Your access ended before these changes reached the server. The shared document was not changed by them.",
  became_read_only: "Your role became read-only before these changes reached the server, so the server did not accept them.",
};

/**
 * Text this browser typed that the server never received, kept so it is not silently lost (docs/ai.md 19.6).
 * It is shown to the person who typed it and is never merged back by itself: they can read it, copy it, put it into
 * the current document on purpose (when they may edit), or discard it after confirming.
 */
export function RecoveryPanel(props: {
  userId: string;
  roomId: string;
  /** document titles by id, for records whose document is not the open one */
  titles: ReadonlyMap<string, string>;
  /** may this record be put into the document that is open now? */
  canInsert: (record: RecoveryRecord) => boolean;
  /** put the record's text into the open document; true when it was inserted */
  onInsert: (record: RecoveryRecord) => boolean;
}) {
  useSyncExternalStore(recoveryStore.subscribe, recoveryStore.getVersion);
  const records = recoveryStore.list(props.userId, props.roomId);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [note, setNote] = useState<{ id: string; text: string } | null>(null);
  if (records.length === 0) return null;

  async function copy(record: RecoveryRecord) {
    const text = record.blocks.map((b) => b.inserted).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setNote({ id: record.id, text: "Copied to the clipboard." });
    } catch {
      setNote({ id: record.id, text: "Could not use the clipboard; select the text above and copy it." });
    }
  }

  return (
    <section className="recovery" role="region" aria-label="Recovered text" data-testid="recovery-panel">
      <h2>Unsent text was kept ({records.length})</h2>
      {!recoveryStore.persisted && (
        <p className="error" role="alert">
          This browser would not store these copies, so they only last until you close this page. Copy anything you need now.
        </p>
      )}
      <ul>
        {records.map((record) => {
          const title = props.titles.get(record.documentId) ?? record.documentTitle;
          const insertable = props.canInsert(record);
          return (
            <li key={record.id} data-testid="recovery-record">
              <p>
                <strong>{title}</strong>{" "}
                <time dateTime={record.capturedAt}>{new Date(record.capturedAt).toLocaleString()}</time>
              </p>
              <p className="muted">{WHY[record.reason]}</p>
              {record.blocks.map((block, i) => (
                <div key={i} className="recovered-block">
                  <label>
                    <span className="muted">Your unsent text{record.blocks.length > 1 ? ` (${i + 1} of ${record.blocks.length})` : ""}</span>
                    <textarea readOnly rows={Math.min(6, Math.max(2, block.inserted.split("\n").length))} value={block.inserted} />
                  </label>
                  {block.yourVersion !== block.inserted && (
                    <details>
                      <summary>Your whole paragraph as you had it</summary>
                      <textarea readOnly rows={3} value={block.yourVersion} aria-label="Your paragraph as you had it" />
                    </details>
                  )}
                  {block.truncated && <p className="muted">This text was very long and was cut at {20_000} characters.</p>}
                </div>
              ))}
              <div className="actions">
                <button type="button" onClick={() => void copy(record)}>
                  Copy
                </button>
                <button
                  type="button"
                  disabled={!insertable}
                  title={insertable ? undefined : "Open this document with edit access to put the text back into it."}
                  onClick={() => {
                    if (props.onInsert(record)) recoveryStore.remove(props.userId, record.id);
                    else setNote({ id: record.id, text: "The document could not take it right now. Nothing was removed." });
                  }}
                >
                  Insert into the document
                </button>
                {confirming === record.id ? (
                  <>
                    <span role="alert">Discard this text for good?</span>
                    <button type="button" className="danger" onClick={() => (recoveryStore.remove(props.userId, record.id), setConfirming(null))}>
                      Yes, discard it
                    </button>
                    <button type="button" onClick={() => setConfirming(null)}>
                      Keep it
                    </button>
                  </>
                ) : (
                  <button type="button" onClick={() => setConfirming(record.id)}>
                    Discard…
                  </button>
                )}
              </div>
              {note?.id === record.id && (
                <p role="status" className="muted">
                  {note.text}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
