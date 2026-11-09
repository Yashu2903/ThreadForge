/** Shapes returned by the backend (see backend/app/schemas/). */
export interface Me {
  id: string;
  display_name: string;
  email: string | null;
  email_verified: boolean;
}

export interface RoomSummary {
  id: string;
  name: string;
  owner_id: string;
  role: "owner" | "editor" | "viewer";
  active_document_id: string | null;
  active_document_version: number;
}

export interface DocumentSummary {
  id: string;
  room_id: string;
  title: string;
  template_key: string;
  schema_version: number;
  metadata_version: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  is_active: boolean;
  requires_review: boolean;
  /** legacy: not opened yet; ready: live in the collaboration service; needs_repair: refused */
  collab_state: "legacy" | "ready" | "needs_repair";
}

/** One document as the API describes it. There is NO text here: the editor receives the text from
 *  the collaboration service over `collab_path`, joining the document called `document_name`. */
export interface DocumentFull extends DocumentSummary {
  can_edit: boolean;
  collab_path: string;
  document_name: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface Capabilities {
  google_drive: "available" | "not_configured";
}

export const TEMPLATES = [
  { key: "blank", label: "Blank document", detail: "One empty paragraph." },
  { key: "project_brief", label: "Project brief", detail: "Objective, Scope, Requirements, Next steps." },
  { key: "technical_proposal", label: "Technical proposal", detail: "Problem, Proposed approach, Architecture, Tradeoffs, Implementation plan." },
  { key: "meeting_notes", label: "Meeting notes", detail: "Agenda, Discussion, Decisions, Action items." },
  { key: "research_summary", label: "Research summary", detail: "Research question, Findings, Evidence, Limitations, Conclusions." },
] as const;
export type TemplateKey = (typeof TEMPLATES)[number]["key"];
