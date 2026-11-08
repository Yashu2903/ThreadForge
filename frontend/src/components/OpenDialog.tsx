import type { DocumentStore } from "../documents/store";
import { useStore } from "../useRoom";
import type { Capabilities } from "../types";
import { Dialog } from "./Dialog";

/**
 * Open a document. Sections appear only when they work: "Saved documents" now; "Upload from this
 * device" arrives with DOCX import and the Drive picker with the Drive checkpoint. Google Drive is
 * shown (as unavailable, with a setup explanation) when the server reports it is not configured.
 */
export function OpenDialog(props: {
  store: DocumentStore;
  isOwner: boolean;
  capabilities: Capabilities | null;
  onOpen: (id: string) => void;
  onMakeActive: (id: string) => Promise<void>;
  onClose: () => void;
}) {
  const { list } = useStore(props.store);
  return (
    <Dialog title="Open a document" onClose={props.onClose}>
      <section aria-labelledby="saved-heading">
        <h3 id="saved-heading">Saved documents</h3>
        {list.error && (
          <p role="alert" className="error">
            {list.error}
          </p>
        )}
        {list.items.length === 0 && !list.loading && !list.error && <p className="muted">This room has no documents yet.</p>}
        <ul className="doc-list">
          {list.items.map((doc) => (
            <li key={doc.id}>
              <div>
                <strong>{doc.title}</strong>
                {doc.is_active && <span className="badge">Active</span>}
                <small>Updated {new Date(doc.updated_at).toLocaleString()}</small>
              </div>
              <div className="row-actions">
                <button onClick={() => props.onOpen(doc.id)}>Open</button>
                {props.isOwner && !doc.is_active && (
                  <button onClick={() => void props.onMakeActive(doc.id)} title="Everyone in the room will see this document">
                    Make active
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
        {list.nextCursor && (
          <button onClick={() => void props.store.loadMore()} disabled={list.loading}>
            {list.loading ? "Loading…" : "Load more"}
          </button>
        )}
      </section>
      {props.capabilities?.google_drive === "not_configured" && (
        <section aria-labelledby="drive-heading" className="unavailable">
          <h3 id="drive-heading">Google Drive</h3>
          <p>
            <strong>Not available.</strong> Importing from Google Drive needs the server to be set up with a Google
            Cloud project: GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET, GOOGLE_PICKER_API_KEY,
            GOOGLE_CLOUD_PROJECT_NUMBER and CLOUD_TOKEN_ENCRYPTION_KEYS. See the README, "Google Drive setup".
          </p>
        </section>
      )}
      <div className="actions">
        <button onClick={props.onClose}>Close</button>
      </div>
    </Dialog>
  );
}
