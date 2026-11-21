/**
 * The Files panel (sources) and the AI settings dialog (data notice, personal keys, the owner's switch,
 * usage). Controls that have no backend behind them are not shown: there is no Drive picker, no image
 * generation, no per-source privacy toggle in this phase.
 */
import { useRef, useState } from "react";
import { Dialog } from "../components/Dialog";
import { ApiError } from "../api";
import { Menu, Popover, Tip } from "../ui/Popover";
import { IconFile, IconFilePdf, IconFileText, IconInfo, IconMore, IconPlus, IconRefresh } from "../ui/icons";
import { type AiSnapshot, type AiStore, unavailableReason } from "./store";
import { ERROR_TEXT, type AnalysisInfo, type Passage, type SourceItem } from "./types";

const STATE_TEXT: Record<string, string> = {
  not_analyzed: "Not analyzed yet",
  queued: "Queued",
  extracting: "Reading the file",
  indexing: "Indexing",
  ready: "Ready",
  failed: "Failed",
  removed: "Removed",
};

function fail(err: unknown): string {
  if (err instanceof ApiError) return ERROR_TEXT[err.code] ?? err.message;
  return "Could not reach the server.";
}

function fileIcon(name: string, kind?: string) {
  const ext = (kind ?? name.split(".").pop() ?? "").toLowerCase();
  if (ext === "pdf") return <IconFilePdf size={16} />;
  if (ext === "docx") return <IconFile size={16} />;
  return <IconFileText size={16} />;
}

/**
 * The room's files, as compact rows in the sidebar: a file-type icon, the name (it truncates, the full name is in the tooltip), the
 * processing state in small text, one quick action when it applies (Analyze now), and everything else in a menu (Inspect, Download,
 * Replace, Cancel analysis, Remove). Which actions exist still depends on the person's role and the file's state; the server decides.
 * Uploading is a "+" in the header (the composer's attach control uploads too).
 */
export function SourcePanel(props: { store: AiStore; snap: AiSnapshot; role: string | null; documentId: string | null; userId: string }) {
  const { store, snap, role } = props;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inspect, setInspect] = useState<SourceItem | null>(null);
  const replaceInputs = useRef(new Map<string, HTMLInputElement>());
  const list = snap.sources;
  const canEdit = role === "owner" || role === "editor";
  const aiBlocked = unavailableReason(snap, role);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(fail(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="files side-section" aria-label="Files">
      <header className="side-heading">
        <h2 className="category">Files</h2>
        {list && (
          <Popover label="About files" panelLabel="About files" align="end" triggerClassName="icon-button" trigger={<IconInfo size={16} />}>
            {() => (
              <div className="info-popover">
                <p>PDF, Word (.docx), text and Markdown, up to {Math.round(list.limits.max_upload_bytes / 1048576)} MB.</p>
                <p>Uploading does not analyze a file or send anything to an AI provider. A file is analyzed when you ask the AI about it or choose “Analyze now”.</p>
              </div>
            )}
          </Popover>
        )}
        {canEdit && list?.can_upload && (
          <Tip label="Upload a file" side="bottom">
            <label className="icon-button upload-button">
              <IconPlus size={16} />
              <input
                type="file"
                className="sr-only"
                accept=".pdf,.docx,.txt,.md"
                aria-label="Upload a file"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (f) void run(() => store.uploadFile(f));
                }}
              />
            </label>
          </Tip>
        )}
      </header>
      {list === null && <p className="muted small side-empty">Loading…</p>}
      {list && list.sources.length === 0 && <p className="muted small side-empty">No files yet.{canEdit && list.can_upload ? " Upload one with +." : ""}</p>}
      <ul className="source-list compact-list">
        {list?.sources.map((s) => {
          const v = s.current_version;
          const mine = s.created_by === props.userId;
          const state = v?.analysis_state ?? "";
          const running = ["queued", "extracting", "indexing"].includes(state);
          const canAnalyze = !!v && canEdit && ["not_analyzed", "failed"].includes(state);
          const canRemove = role === "owner" || (role === "editor" && mine);
          const statusText = v ? `${STATE_TEXT[state]}${v.error_code ? ` · ${ERROR_TEXT[v.error_code] ?? v.error_code}` : ""}` : "";
          return (
            <li key={s.id} className="source-row">
              <span className="file-icon">{fileIcon(s.display_name, v?.kind)}</span>
              <span className="source-text">
                <strong title={s.display_name}>{s.display_name}</strong>
                {v && <small className={`status-text state-${state}`}>{statusText}</small>}
              </span>
              {canAnalyze && (
                <Tip label={aiBlocked ?? "Analyze this file now"} side="bottom">
                  <button
                    type="button"
                    className="icon-button"
                    aria-label="Analyze now"
                    disabled={busy || !props.documentId || Boolean(aiBlocked)}
                    onClick={() => void run(() => store.analyzeNow(s.id, v!.id, props.documentId!))}
                  >
                    <IconRefresh size={16} />
                  </button>
                </Tip>
              )}
              {v && (canEdit || list.can_download || canRemove) && (
                <Menu
                  label={`Actions for ${s.display_name}`}
                  align="end"
                  triggerClassName="icon-button row-menu"
                  trigger={<IconMore size={16} />}
                  items={[
                    { id: "inspect", label: "Inspect extracted text", hidden: !canEdit, onSelect: () => setInspect(s) },
                    { id: "cancel", label: "Cancel analysis", hidden: !(canEdit && running), disabled: busy, onSelect: () => void run(() => store.cancelAnalysis(s.id, v.id)) },
                    { id: "download", label: "Download", hidden: !list.can_download, onSelect: () => window.location.assign(`/rooms/${store.roomId}/sources/${s.id}/versions/${v.id}/download`) },
                    { id: "replace", label: "Replace with a new version…", hidden: !canEdit, onSelect: () => replaceInputs.current.get(s.id)?.click() },
                    {
                      id: "remove",
                      label: "Remove",
                      danger: true,
                      separatorBefore: true,
                      hidden: !canRemove,
                      disabled: busy,
                      onSelect: () => {
                        if (window.confirm(`Remove “${s.display_name}”? Its text and images are deleted now. Published answers keep their text.`)) {
                          void run(() => store.removeSource(s.id));
                        }
                      },
                    },
                  ]}
                />
              )}
              {canEdit && (
                <input
                  ref={(el) => {
                    if (el) replaceInputs.current.set(s.id, el);
                    else replaceInputs.current.delete(s.id);
                  }}
                  type="file"
                  className="sr-only"
                  tabIndex={-1}
                  accept=".pdf,.docx,.txt,.md"
                  aria-label={`Replace ${s.display_name}`}
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    e.target.value = "";
                    if (f) void run(() => store.uploadFile(f, s.id));
                  }}
                />
              )}
            </li>
          );
        })}
      </ul>
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
      {inspect && <InspectDialog store={store} source={inspect} onClose={() => setInspect(null)} />}
    </section>
  );
}

function InspectDialog(props: { store: AiStore; source: SourceItem; onClose: () => void }) {
  const { store, source } = props;
  const v = source.current_version!;
  const [info, setInfo] = useState<AnalysisInfo | null>(null);
  const [passages, setPassages] = useState<Passage[] | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setError(null);
    try {
      setInfo(await store.analysis(source.id, v.id));
      setPassages((await store.passages(source.id, v.id, -1, q)).passages);
    } catch (err) {
      setError(fail(err));
    }
  }

  return (
    <Dialog title={`Inspect ${source.display_name}`} onClose={props.onClose}>
      <p className="muted">
        {STATE_TEXT[v.analysis_state]} · {v.kind.toUpperCase()}
        {v.page_count ? ` · ${v.page_count} pages` : ""}
      </p>
      <div className="inline-form">
        <label className="sr-only" htmlFor="passage-q">
          Search the extracted text
        </label>
        <input id="passage-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search the extracted text" />
        <button onClick={() => void load()}>{passages ? "Search" : "Show extracted text"}</button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {info && (info.warnings.length > 0 || info.gaps.length > 0) && (
        <div className="conflict" role="status">
          {info.gaps.length > 0 && <p>{info.gaps.length} page(s) could not be read: {info.gaps.map((g) => g.page).filter(Boolean).join(", ")}.</p>}
          {info.warnings.map((w) => (
            <p key={w.code}>{warningText(w)}</p>
          ))}
        </div>
      )}
      {passages && passages.length === 0 && <p className="muted">Nothing extracted yet. Attach the file to an AI request, or choose “Analyze now”.</p>}
      <ol className="passages">
        {passages?.map((p) => (
          <li key={p.id}>
            <p className="muted small">
              {p.kind === "figure" ? `Figure ${p.figure_number}` : `Passage ${p.ordinal + 1}`}
              {p.locator.page_start ? ` · page ${p.locator.page_start}` : ""}
              {p.locator.heading_path?.length ? ` · ${p.locator.heading_path.join(" › ")}` : ""} · {p.extraction.replace("_", " ")}
              {p.ocr_confidence !== null ? ` (${Math.round(p.ocr_confidence)}%)` : ""}
              {p.flags.low_agreement ? " · low agreement" : ""}
            </p>
            <p>{p.text}</p>
          </li>
        ))}
      </ol>
      <div className="actions">
        <button onClick={props.onClose}>Close</button>
      </div>
    </Dialog>
  );
}

function warningText(w: { code: string; [k: string]: unknown }): string {
  switch (w.code) {
    case "figures_capped":
      return `${w.analyzed} of ${w.found} figures were analyzed.`;
    case "chunks_capped":
      return `Only the first ${w.indexed} of ${w.found} passages were indexed.`;
    case "unreadable_pages":
      return `${w.count} page(s) could not be read.`;
    case "figures_not_analyzed":
      return "Figures were not analyzed because the vision model is unavailable.";
    case "low_agreement":
      return `Page ${w.page}: two readings of the scan disagreed.`;
    case "image_embedding_unavailable":
      return "Figures were indexed by their descriptions only.";
    default:
      return w.code.replace(/_/g, " ");
  }
}

export function AiSettingsDialog(props: { store: AiStore; snap: AiSnapshot; isOwner: boolean; onClose: () => void }) {
  const { store, snap } = props;
  const [provider, setProvider] = useState<"openai" | "google">("openai");
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [usage, setUsage] = useState<Record<string, unknown> | null>(null);
  const status = snap.status;

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(fail(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title="AI settings" onClose={props.onClose} busy={busy}>
      {status && !status.available && (
        <p className="unavailable" role="status">
          {status.code === "ai_not_configured" && "AI is not set up on this server."}
          {status.code === "ai_disabled" && "AI is switched off for the whole service."}
          {status.code === "ai_unavailable" && "AI is temporarily unavailable: a provider check failed."}
        </p>
      )}
      {status?.available && snap.settings && !snap.settings.ai_enabled && <p className="unavailable">The room owner switched AI off in this room.</p>}

      <h3>Data notice</h3>
      <ol>
        {snap.consent?.points.map((p) => (
          <li key={p}>{p}</li>
        ))}
      </ol>
      {snap.consent?.accepted ? (
        <p className="muted">You accepted this notice.</p>
      ) : (
        <button className="primary" disabled={busy} onClick={() => void run(() => store.acceptConsent())}>
          I have read it and agree
        </button>
      )}

      {status?.key_policy !== "server_only" && (
        <>
          <h3>Your API keys</h3>
          <p className="muted">
            Requests use your key when you saved one, otherwise the server's. Keys are stored encrypted and are never shown again.
          </p>
          <ul className="source-list">
            {snap.keys.map((k) => (
              <li key={k.provider}>
                {k.provider === "openai" ? "OpenAI" : "Google"} · ends in {k.last4} {k.state === "rejected" && <span className="error">· rejected, replace it</span>}
                <button disabled={busy} onClick={() => void run(() => store.deleteKey(k.provider))}>
                  Delete
                </button>
              </li>
            ))}
          </ul>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                await store.saveKey(provider, key.trim());
                setKey("");
              });
            }}
          >
            <label className="sr-only" htmlFor="key-provider">
              Provider
            </label>
            <select id="key-provider" value={provider} onChange={(e) => setProvider(e.target.value as "openai" | "google")}>
              <option value="openai">OpenAI (answers)</option>
              <option value="google">Google (search embeddings)</option>
            </select>
            <label className="sr-only" htmlFor="key-value">
              API key
            </label>
            <input id="key-value" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder="Paste a key" />
            <button type="submit" className="primary" disabled={busy || key.trim().length < 16}>
              Save key
            </button>
          </form>
        </>
      )}

      {props.isOwner && snap.settings && (
        <>
          <h3>Room</h3>
          <label className="choice">
            <input
              type="checkbox"
              checked={snap.settings.ai_enabled}
              disabled={busy}
              onChange={(e) => void run(() => store.setRoomSwitch(e.target.checked))}
            />
            <span>
              Allow AI in this room
              <small>Off: no new requests or analysis. Files can still be uploaded.</small>
            </span>
          </label>
          <button disabled={busy} onClick={() => void run(async () => setUsage(await store.usage()))}>
            Show this room's AI usage
          </button>
          {usage && <UsageView usage={usage} />}
        </>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button onClick={props.onClose}>Close</button>
      </div>
    </Dialog>
  );
}

/** What a room spent. Cost is only shown as a number when it was priced: a call that used tokens but could not be
 *  priced is UNKNOWN, never $0 (the server says so in `cost_status`: none | estimated | partial | unpriced). */
export function costSentence(usage: Record<string, unknown>): string {
  const totals = (usage.totals ?? {}) as Record<string, number>;
  const status = (usage.cost_status ?? "estimated") as string;
  const dollars = `$${((totals.cost_micros ?? 0) / 1e6).toFixed(4)}`;
  if (status === "unpriced") return "cost unknown (no prices are configured, so usage is counted but not priced)";
  if (status === "partial") return `${dollars} so far, and more that could not be priced (partial, not a total)`;
  if (status === "none") return "no cost";
  return `about ${dollars}`;
}

export function UsageView({ usage }: { usage: Record<string, unknown> }) {
  const totals = (usage.totals ?? {}) as Record<string, number>;
  const warnings = (usage.warnings ?? []) as { scope: string; threshold: number }[];
  return (
    <div className="usage" data-testid="usage">
      <p>
        Last 30 days: {totals.calls ?? 0} calls, {costSentence(usage)}.
      </p>
      {warnings.length > 0 && (
        <p className="error" role="status">
          Budget warnings: {warnings.map((w) => `${w.scope} ${w.threshold}%`).join(", ")}
        </p>
      )}
    </div>
  );
}
