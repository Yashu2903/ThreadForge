/**
 * The AI state of one open room: status, consent, the owner's switch, personal keys, the files, and
 * the requests/results the chat cards show.
 *
 * It is a small external store (subscribe/getSnapshot, like DocumentStore). It never holds a provider
 * key and never decides anything: every permission, limit and state comes from the backend; this file
 * only fetches, caches and reacts to the room client's AI signals (`ai_request_updated`,
 * `source_updated`, `room_ai_settings_changed`). Events carry ids and states only, so every signal
 * triggers a fetch of the authorized endpoint.
 *
 * Request cards poll while they are visible and unfinished, with a growing delay (2 s ... 15 s); the
 * events make most polls unnecessary, the polling is the safety net and the source of the stage labels.
 */
import type { AiSignals } from "threadforge-client";
import { type Api, ApiError } from "../api";
import type {
  AiResult,
  AiStatus,
  AnalysisInfo,
  Consent,
  EstimateReply,
  KeyMeta,
  Passage,
  RequestInfo,
  RoomAiSettings,
  SourceList,
} from "./types";

export const TERMINAL = new Set(["succeeded", "failed", "cancelled", "expired"]);

export interface AiSnapshot {
  version: number;
  status: AiStatus | null;
  consent: Consent | null;
  settings: RoomAiSettings | null;
  keys: KeyMeta[];
  sources: SourceList | null;
  loadError: string | null;
}

export interface SendBody {
  client_request_id: string;
  instruction: string;
  task_hint: "auto" | "ask" | "summarize" | "compare";
  document_id: string;
  include_document_content: boolean;
  document_scope: "none" | "whole" | "blocks";
  selected_block_ids: string[];
  selection_hash: string | null;
  source_version_ids: string[];
  include_chat_context: boolean;
  chat_message_ids: string[];
  confirm_large: boolean;
}

export class AiStore {
  private version = 0;
  private listeners = new Set<() => void>();
  private snapshot: AiSnapshot = { version: 0, status: null, consent: null, settings: null, keys: [], sources: null, loadError: null };
  readonly requests = new Map<string, RequestInfo | "forbidden" | "missing">();
  readonly results = new Map<string, AiResult | "missing">();
  private polls = new Map<string, { timer: ReturnType<typeof setTimeout>; step: number }>();
  private disposed = false;

  constructor(
    private readonly api: Api,
    readonly roomId: string,
    readonly userId: string,
  ) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): AiSnapshot => this.snapshot;

  private emit(patch: Partial<AiSnapshot> = {}): void {
    this.version += 1;
    this.snapshot = { ...this.snapshot, ...patch, version: this.version };
    for (const l of [...this.listeners]) l();
  }

  /** React StrictMode mounts, unmounts and re-mounts an effect with the SAME store: a disposed store
   *  must come back to life when the effect runs again. */
  activate(): void {
    this.disposed = false;
  }

  dispose(): void {
    this.disposed = true;
    for (const p of this.polls.values()) clearTimeout(p.timer);
    this.polls.clear();
  }

  // ---- loading ----------------------------------------------------------------------------------------

  async load(): Promise<void> {
    try {
      const [status, consent, settings, keys, sources] = await Promise.all([
        this.api.get<AiStatus>("/ai/status"),
        this.api.get<Consent>("/ai/consent"),
        this.api.get<RoomAiSettings>(`/rooms/${this.roomId}/ai-settings`),
        this.api.get<{ items: KeyMeta[] }>("/ai/keys"),
        this.api.get<SourceList>(`/rooms/${this.roomId}/sources`),
      ]);
      if (this.disposed) return;
      this.emit({
        status: status.body,
        consent: consent.body,
        settings: settings.body,
        keys: keys.body.items,
        sources: sources.body,
        loadError: null,
      });
    } catch (err) {
      if (!this.disposed) this.emit({ loadError: err instanceof ApiError ? err.message : "Could not reach the server." });
    }
  }

  async refreshSources(): Promise<void> {
    try {
      const res = await this.api.get<SourceList>(`/rooms/${this.roomId}/sources`);
      if (!this.disposed) this.emit({ sources: res.body });
    } catch {
      /* the next signal or poll retries */
    }
  }

  /** The room client's AI signals: ids and states only, so each one is a fetch. */
  applySignals = async (signals: AiSignals): Promise<void> => {
    if (signals.reset) {
      this.requests.clear();
      this.results.clear();
      await this.load();
      return;
    }
    if (signals.aiEnabled !== null && this.snapshot.settings) {
      this.emit({ settings: { ...this.snapshot.settings, ai_enabled: signals.aiEnabled } });
      void this.refreshSettings();
    }
    if (signals.sources.size > 0) await this.refreshSources();
    await Promise.all([...signals.requests.keys()].map((id) => this.refreshRequest(id)));
  };

  async refreshSettings(): Promise<void> {
    try {
      const res = await this.api.get<RoomAiSettings>(`/rooms/${this.roomId}/ai-settings`);
      if (!this.disposed) this.emit({ settings: res.body });
    } catch {
      /* ignore */
    }
  }

  // ---- requests and results ----------------------------------------------------------------------------

  async refreshRequest(id: string): Promise<RequestInfo | "forbidden" | "missing"> {
    let next: RequestInfo | "forbidden" | "missing";
    try {
      next = (await this.api.get<RequestInfo>(`/rooms/${this.roomId}/ai-requests/${id}`)).body;
    } catch (err) {
      // viewers (and editors without write access) see only the ordinary chat message
      next = err instanceof ApiError && err.status === 403 ? "forbidden" : "missing";
    }
    if (this.disposed) return next;
    this.requests.set(id, next);
    if (typeof next !== "string" && next.state === "succeeded") void this.ensureResult(id, true);
    this.emit();
    return next;
  }

  /** Called by a card that is on screen: fetch once, then keep polling until the request ends. */
  watch(id: string): void {
    if (this.disposed || this.polls.has(id)) return;
    const known = this.requests.get(id);
    if (known === "forbidden" || (typeof known === "object" && TERMINAL.has(known.state))) return;
    this.polls.set(id, { timer: setTimeout(() => void this.poll(id), 0), step: 0 });
  }

  private async poll(id: string): Promise<void> {
    const current = this.polls.get(id);
    if (!current || this.disposed) return;
    const info = await this.refreshRequest(id);
    if (typeof info === "string" || TERMINAL.has(info.state)) {
      this.polls.delete(id);
      return;
    }
    const delay = Math.min(15_000, 2_000 + current.step * 1_500);
    current.step += 1;
    current.timer = setTimeout(() => void this.poll(id), delay);
  }

  async ensureResult(id: string, force = false): Promise<AiResult | "missing"> {
    const cached = this.results.get(id);
    if (cached && !force) return cached;
    let value: AiResult | "missing";
    try {
      value = (await this.api.get<AiResult>(`/rooms/${this.roomId}/ai-requests/${id}/result`)).body;
    } catch {
      value = "missing";
    }
    if (this.disposed) return value;
    this.results.set(id, value);
    this.emit();
    return value;
  }

  // ---- actions -------------------------------------------------------------------------------------------

  async estimate(body: Omit<SendBody, "client_request_id" | "confirm_large" | "selected_block_ids" | "selection_hash">, selectedChars: number) {
    const res = await this.api.send<EstimateReply>("POST", `/rooms/${this.roomId}/ai-requests/estimate`, {
      instruction: body.instruction,
      task_hint: body.task_hint,
      document_id: body.document_id,
      include_document_content: body.include_document_content,
      document_scope: body.document_scope,
      selected_chars: selectedChars,
      include_chat_context: body.include_chat_context,
      chat_message_ids: body.chat_message_ids,
      source_version_ids: body.source_version_ids,
    });
    return res.body;
  }

  async send(body: SendBody): Promise<RequestInfo> {
    const res = await this.api.send<RequestInfo>("POST", `/rooms/${this.roomId}/ai-requests`, body);
    this.requests.set(res.body.id, res.body);
    this.emit();
    this.watch(res.body.id);
    return res.body;
  }

  async cancel(id: string): Promise<void> {
    await this.api.send("POST", `/rooms/${this.roomId}/ai-requests/${id}/cancel`);
    await this.refreshRequest(id);
    this.polls.delete(id);
    this.watch(id);
  }

  async retry(id: string): Promise<void> {
    await this.api.send("POST", `/rooms/${this.roomId}/ai-requests/${id}/retry`);
    await this.refreshRequest(id);
    this.polls.delete(id);
    this.watch(id);
  }

  async analyzeNow(sourceId: string, versionId: string, documentId: string): Promise<void> {
    await this.api.send("POST", `/rooms/${this.roomId}/sources/${sourceId}/versions/${versionId}/analyze`, { document_id: documentId });
    await this.refreshSources();
  }

  async cancelAnalysis(sourceId: string, versionId: string): Promise<void> {
    await this.api.send("DELETE", `/rooms/${this.roomId}/sources/${sourceId}/versions/${versionId}/analyze`);
    await this.refreshSources();
  }

  async removeSource(sourceId: string): Promise<void> {
    await this.api.send("DELETE", `/rooms/${this.roomId}/sources/${sourceId}`);
    await this.refreshSources();
  }

  async analysis(sourceId: string, versionId: string): Promise<AnalysisInfo> {
    return (await this.api.get<AnalysisInfo>(`/rooms/${this.roomId}/sources/${sourceId}/versions/${versionId}/analysis`)).body;
  }

  async passages(sourceId: string, versionId: string, after = -1, q = ""): Promise<{ passages: Passage[]; next_after: number | null }> {
    const params = new URLSearchParams({ after: String(after), limit: "30" });
    if (q.trim()) params.set("q", q.trim());
    return (await this.api.get<{ passages: Passage[]; next_after: number | null }>(`/rooms/${this.roomId}/sources/${sourceId}/versions/${versionId}/passages?${params}`)).body;
  }

  /** The three-step device upload (intent, bytes, finalize). `replaceSourceId` makes a new version. */
  async uploadFile(file: File, replaceSourceId?: string): Promise<void> {
    const intent = await this.api.send<{ id: string }>("POST", `/rooms/${this.roomId}/source-uploads`, {
      client_request_id: crypto.randomUUID(),
      filename: file.name,
      byte_size: file.size,
      ...(file.type ? { media_type: file.type } : {}),
      ...(replaceSourceId ? { source_id: replaceSourceId } : {}),
    });
    const uploadId = intent.body.id;
    await this.api.upload(`/rooms/${this.roomId}/source-uploads/${uploadId}/content`, file, "application/octet-stream", "PUT");
    await this.api.send("POST", `/rooms/${this.roomId}/source-uploads/${uploadId}/finalize`);
    await this.refreshSources();
  }

  async saveKey(provider: "openai" | "google", apiKey: string): Promise<void> {
    await this.api.send("PUT", `/ai/keys/${provider}`, { api_key: apiKey });
    await this.load();
  }
  async deleteKey(provider: "openai" | "google"): Promise<void> {
    await this.api.send("DELETE", `/ai/keys/${provider}`);
    await this.load();
  }
  async acceptConsent(): Promise<void> {
    const version = this.snapshot.consent?.notice_version ?? this.snapshot.status?.notice_version;
    await this.api.send("POST", "/ai/consent", { notice_version: version });
    await this.load();
  }
  async setRoomSwitch(enabled: boolean): Promise<void> {
    await this.api.send("PUT", `/rooms/${this.roomId}/ai-settings`, { ai_enabled: enabled });
    await this.refreshSettings();
  }
  async usage(): Promise<Record<string, unknown>> {
    return (await this.api.get<Record<string, unknown>>(`/rooms/${this.roomId}/ai-usage?days=30`)).body;
  }
}

/** Why AI cannot be used right now, in words, or null when it can. */
export function unavailableReason(snap: AiSnapshot, role: string | null): string | null {
  if (!snap.status || !snap.settings) return null;
  if (role === "viewer") return "AI requests need an editor or owner role. You can still read answers.";
  if (snap.status.code === "ai_not_configured") return "AI is not set up on this server.";
  if (snap.status.code === "ai_disabled") return "AI is switched off for the whole service.";
  if (snap.status.code === "ai_unavailable") return "AI is temporarily unavailable (a provider check is failing).";
  if (!snap.settings.ai_enabled) return "The room owner switched AI off in this room. Files can still be uploaded.";
  const unusable = snap.consent && Object.values(snap.consent.key_sources).find((k) => k.source === null);
  if (unusable) return "No API key is available. Add your own key in AI settings.";
  return null;
}
