import { useRef, useState } from "react";
import { type Api, ApiError } from "../api";
import { type DocumentFull, TEMPLATES, type TemplateKey } from "../types";
import { Dialog } from "./Dialog";

/**
 * Start a document: title + template. Submitting is guarded against doubles, and a retry after a
 * failure re-sends the SAME client_request_id, so the server answers a request that actually landed
 * with the existing document instead of making a second one.
 */
export function StartDialog(props: {
  api: Api;
  roomId: string;
  onCreated: (doc: DocumentFull) => void;
  onClose: () => void;
}) {
  const [title, setTitle] = useState("");
  const [template, setTemplate] = useState<TemplateKey>("blank");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(crypto.randomUUID());
  const sent = useRef<{ title: string; template: TemplateKey } | null>(null);
  const inFlight = useRef(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = title.trim();
    if (inFlight.current) return;
    if (!trimmed) return setError("Give the document a title.");
    if (Array.from(trimmed).length > 200) return setError("A title can be at most 200 characters.");
    // a changed title/template is a different request, so it gets a new id; an unchanged retry keeps it
    if (sent.current && (sent.current.title !== trimmed || sent.current.template !== template)) {
      requestId.current = crypto.randomUUID();
    }
    sent.current = { title: trimmed, template };
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const res = await props.api.send<DocumentFull>("POST", `/rooms/${props.roomId}/documents`, {
        title: trimmed,
        template_key: template,
        client_request_id: requestId.current,
      });
      props.onCreated(res.body);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not reach the server. Try again: nothing is duplicated if the first attempt got through.",
      );
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <Dialog title="Start a document" onClose={props.onClose} busy={busy}>
      <form onSubmit={submit}>
        <label className="field">
          <span>Title</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={400} disabled={busy} autoComplete="off" />
        </label>
        <fieldset className="field" disabled={busy}>
          <legend>Template</legend>
          {TEMPLATES.map((t) => (
            <label key={t.key} className="choice">
              <input type="radio" name="template" value={t.key} checked={template === t.key} onChange={() => setTemplate(t.key)} />
              <span>
                <strong>{t.label}</strong>
                <small>{t.detail}</small>
              </span>
            </label>
          ))}
        </fieldset>
        <p className="hint">The new document becomes the room's active document.</p>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" onClick={props.onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy}>
            {busy ? "Creating…" : "Create document"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
