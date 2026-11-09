/** Shapes returned by the AI endpoints (see backend/app/api/ai*.py, sources.py, analysis.py). */

export interface AiStatus {
  available: boolean;
  /** ok | ai_not_configured | ai_disabled | ai_unavailable */
  code: string;
  key_policy: string;
  notice_version: number;
  capabilities: Record<string, { state: "ok" | "failed" | "not_checked"; code: string }>;
  spend_controls: { prices_configured: boolean; budgets_enforced: boolean };
}

export interface Consent {
  notice_version: number;
  accepted: boolean;
  points: string[];
  key_sources: Record<string, { provider: string; source: "server" | "user" | null; problem: string | null }>;
}

export interface KeyMeta {
  provider: "openai" | "google";
  last4: string;
  state: "active" | "rejected";
  last_validated_at: string | null;
}

export interface RoomAiSettings {
  ai_enabled: boolean;
  available: boolean;
  code: string;
}

export interface SourceVersion {
  id: string;
  source_id: string;
  version_no: number;
  original_name: string;
  kind: "pdf" | "docx" | "txt" | "md";
  byte_size: number;
  analysis_state: "not_analyzed" | "queued" | "extracting" | "indexing" | "ready" | "failed" | "removed";
  page_count: number | null;
  likely_scanned_pages: number | null;
  image_count: number;
  text_chars: number;
  error_code: string | null;
}

export interface SourceItem {
  id: string;
  display_name: string;
  created_by: string;
  current_version: SourceVersion | null;
  version_count: number;
  versions?: SourceVersion[];
}

export interface SourceList {
  sources: SourceItem[];
  limits: { max_upload_bytes: number; max_sources: number; room_quota_bytes: number; accepted: string[] };
  used_sources: number;
  used_bytes: number;
  can_upload: boolean;
  can_download: boolean;
}

export interface AnalysisInfo {
  analysis_state: SourceVersion["analysis_state"];
  error_code: string | null;
  job_state: string | null;
  job_stage: string | null;
  chunk_count: number | null;
  figure_count: number | null;
  figures_found: number | null;
  pages_total: number | null;
  pages_unreadable: number | null;
  warnings: { code: string; [k: string]: unknown }[];
  gaps: { page?: number; reason: string }[];
}

export interface Passage {
  id: string;
  ordinal: number;
  kind: "text" | "figure";
  text: string;
  extraction: string;
  ocr_confidence: number | null;
  locator: { page_start?: number; page_end?: number; heading_path?: string[]; figure_number?: number };
  flags: { low_agreement?: boolean };
  figure_number: number | null;
}

export interface Estimate {
  input_tokens: number;
  expected_output_tokens: number;
  provider_calls: number;
  images: number;
  cost_known: boolean;
  expected_cost_micros: number | null;
  max_cost_micros: number | null;
  requires_confirmation: boolean;
  over_caps: string[];
  new_analysis_pages: number;
  new_analysis_figures: number;
}

export interface EstimateReply {
  estimate: Estimate;
  key_sources: Record<string, { provider: string; source: "server" | "user" | null; problem: string | null }>;
  consent_given: boolean;
  can_send: boolean;
  blocking_code: string | null;
}

export type RequestState =
  | "waiting_for_sources"
  | "queued"
  | "running"
  | "validating"
  | "succeeded"
  | "failed"
  | "cancelling"
  | "cancelled"
  | "expired";

export interface RequestInfo {
  id: string;
  message_id: string;
  requested_by: string;
  state: RequestState;
  stage_label: string | null;
  task_type: string | null;
  error_code: string | null;
  error_detail: Record<string, unknown> | null;
  attempt: number;
  max_attempts: number;
  sources: { version_id: string; name: string; version_no: number; analysis_state: string }[];
  queue_position: number | null;
  can_cancel: boolean;
  can_retry: boolean;
  reply_message_id: string | null;
}

export interface Citation {
  n: number;
  label: string;
  kind: "passage" | "section" | "document";
  source_version_id: string | null;
  source_name: string;
  locator: { page_start?: number; page_end?: number; heading_path?: string[]; figure_number?: number; section_key?: string };
  extraction: string;
  machine_generated: boolean;
  low_agreement: boolean;
  figure_chunk_id: string | null;
  excerpt?: string;
  quotes?: string[];
  source_removed?: boolean;
  redacted?: boolean;
}

export type AnswerBlock =
  | { type: "paragraph"; text: string; citations: number[]; unsupported: boolean; note?: string }
  | { type: "bullet_list"; items: { text: string; citations: number[]; unsupported: boolean }[] }
  | {
      type: "diagram";
      title: string;
      mermaid: string;
      citations: number[];
      unsupported: boolean;
      render_status: "ok" | "unchecked" | "failed";
      render_error: string | null;
    };

export interface Coverage {
  task?: string;
  sources?: {
    source_version_id: string;
    source_name: string;
    mode: "full" | "sampled" | "retrieval";
    sections_total: number;
    sections_read: number;
    figures_total: number;
    figures_examined: number;
  }[];
  document?: { mode: string; blocks_total: number; blocks_included: number; captured_update_counter: number; scope: string };
  chat_messages_included?: number;
}

export interface AiResult {
  request_id: string;
  reply_message_id: string | null;
  task_type: string;
  redacted: boolean;
  answer: { blocks: AnswerBlock[]; evidence_gaps: string[] } | null;
  citations: Citation[];
  coverage: Coverage;
  models: Record<string, string>;
  citation_text_visible: boolean;
}

/** Plain-language text for the codes users can actually meet. */
export const ERROR_TEXT: Record<string, string> = {
  source_failed: "One of the files could not be analyzed, so the request was stopped.",
  source_removed: "A file was removed while the request was waiting.",
  too_many_unreadable_pages: "Too many pages of a file could not be read.",
  selection_changed: "The text you selected changed. Select it again and resend.",
  answer_unsupported: "The answer could not be tied to the files closely enough, so it was not posted.",
  answer_invalid: "The model's answer was malformed, so it was not posted.",
  budget_exhausted: "The AI spending budget for this period is used up.",
  key_unavailable: "No API key is available for this request.",
  key_rejected: "The provider rejected the API key.",
  consent_required: "Accept the AI data notice first.",
  ai_write_access_required: "You no longer have write access to the document this request was made from.",
  insufficient_role: "You no longer have a role that may use AI here.",
  ai_disabled_for_room: "The room owner switched AI off.",
  ai_disabled: "AI is switched off for the whole service.",
  ai_unavailable: "AI is temporarily unavailable.",
  request_expired: "The request waited too long and expired.",
  capture_unavailable: "The open document could not be read right now.",
  provider_unavailable: "The AI provider could not be reached.",
};
