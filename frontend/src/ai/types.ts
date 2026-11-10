/** Shapes returned by the AI endpoints (see backend/app/api/ai*.py, sources.py, analysis.py). */

export interface AiStatus {
  available: boolean;
  /** ok | ai_not_configured | ai_disabled | ai_unavailable */
  code: string;
  key_policy: string;
  notice_version: number;
  capabilities: Record<string, { state: "ok" | "failed" | "not_checked"; code: string }>;
  spend_controls: { prices_configured: boolean; budgets_enforced: boolean };
  /** phase 2: the composer offers "Propose document edit" / "Draft new document" only when true */
  proposals_enabled?: boolean;
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
  operation?: "answer" | "edit" | "draft";
  proposal_id?: string | null;
  document_id?: string | null;
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
  proposal_id?: string | null;
  redacted: boolean;
  answer: { blocks: AnswerBlock[]; evidence_gaps: string[] } | null;
  citations: Citation[];
  coverage: Coverage;
  models: Record<string, string>;
  citation_text_visible: boolean;
}

/** A ProseMirror node as the server stores it (a block of a document). */
export interface PMNode {
  type?: string;
  attrs?: Record<string, unknown>;
  content?: PMNode[];
  marks?: { type: string; attrs?: Record<string, unknown> }[];
  text?: string;
}

export interface ProposalChange {
  op: number;
  kind: "replace_block_text" | "replace_range" | "insert_blocks_after" | "delete_blocks";
  block_id: string | null;
  anchor: string | null;
  before: PMNode[];
  after: PMNode[];
  reason: string;
  citations: number[];
  unsupported: boolean;
}

export type ProposalState = "pending" | "applying" | "applied" | "rejected" | "conflicted" | "expired" | "superseded";

export interface ProposalInfo {
  id: string;
  request_id: string;
  kind: "edit" | "draft";
  state: ProposalState;
  document_id: string | null;
  document_title: string | null;
  summary: string;
  operation_count: number;
  preview: {
    kind: "edit" | "draft";
    summary?: string;
    changes?: ProposalChange[];
    title?: string;
    document?: PMNode;
    block_citations?: number[][];
    unsupported_blocks?: number;
    inserted_chars?: number;
  };
  citations: Citation[];
  citation_text_visible: boolean;
  scope: { block_ids?: string[]; ranges?: { block_id: string; start: number; end: number }[]; insert_at?: string | null; anchors?: string[] };
  captured_counter: number | null;
  created_by: string;
  created_at: string;
  expires_at: string;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_code: string | null;
  regenerated_from: string | null;
  published_document_id: string | null;
  /** what THIS person may do with it right now (decided by the server) */
  can_apply?: boolean;
  can_reject?: boolean;
  can_regenerate?: boolean;
  can_publish?: boolean;
}

/** The server's answer to Apply. `synced` = connected editors already have the change; `durable` = it is saved in
 *  the database with its receipt. They are separate facts. */
export interface ApplyReply {
  state: ProposalState;
  already: boolean;
  synced: boolean;
  durable: boolean;
  counter: number | null;
  connected_users: number;
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
  proposal_invalid: "The AI could not produce a change that passes the document's rules, even after one retry, so nothing was proposed.",
  proposal_check_unavailable: "The change checker is not available right now. Try again in a moment.",
  no_evidence: "Nothing relevant was found in the selected files, so no draft was written.",
  edit_target_required: "Select the blocks or text to change, or choose where new text goes. The AI never guesses where to write.",
  ai_proposals_disabled: "Edit proposals and drafts are not switched on on this server.",
  proposal_rate_limited: "You have asked for many proposals in the last hour. Try again a little later.",
  proposal_conflicted: "The document changed since this was proposed, so nothing was changed. Ask for a new proposal from the current text.",
  proposal_expired: "This proposal expired. Ask for a new one.",
  proposal_applied: "This proposal was already applied.",
  proposal_rejected: "This proposal was rejected.",
  proposal_superseded: "A newer proposal replaced this one.",
  proposal_applying: "This proposal is being applied right now. Wait a moment.",
  document_busy: "Someone is editing the document right now. Try again in a few seconds.",
  collab_unavailable: "The editing service could not be reached. Nothing was changed; try again.",
  document_not_found: "The document no longer exists.",
};
