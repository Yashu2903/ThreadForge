/**
 * The two chat cards: the REQUEST card (what the person who asked sees while it runs) and the ANSWER
 * card (what every member sees once it is published).
 *
 * Text is rendered as TEXT. Nothing the model wrote is ever turned into HTML or a link; a diagram goes
 * through SafeMermaid. A citation is a reference, and the card says so: it shows which passage the
 * answer used, not that the passage proves the claim. Machine-read text (OCR, vision, figure
 * descriptions) is labelled as machine-generated. A claim that lost its citation is flagged.
 */
import { useEffect, useState } from "react";
import type { Api } from "../api";
import { ApiError } from "../api";
import { SafeMermaid } from "./SafeMermaid";
import type { AiStore } from "./store";
import { TERMINAL } from "./store";
import { type AiResult, type AnswerBlock, type Citation, type CitationOrigin, ERROR_TEXT, type OriginChunk, type RequestInfo } from "./types";

const STATE_TEXT: Record<string, string> = {
  waiting_for_sources: "Waiting for files",
  queued: "In the queue",
  running: "Working",
  validating: "Checking the answer",
  succeeded: "Done",
  failed: "Could not answer",
  cancelling: "Stopping…",
  cancelled: "Cancelled",
  expired: "Expired",
};

export function RequestCard(props: { store: AiStore; info: RequestInfo }) {
  const { info, store } = props;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const label = info.stage_label ? `${STATE_TEXT[info.state]} · ${info.stage_label}` : STATE_TEXT[info.state] ?? info.state;
  const waitingOn = info.sources.filter((s) => s.analysis_state !== "ready");

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  const reason = info.error_code ? (ERROR_TEXT[info.error_code] ?? "The request could not be completed.") : null;
  return (
    <section className="ai-card request" aria-label="AI request" data-state={info.state}>
      <p className="ai-state" role="status">
        <strong>{label}</strong>
        {info.state === "queued" && info.queue_position ? ` · position ${info.queue_position}` : ""}
        {info.attempt > 1 ? ` · attempt ${info.attempt} of ${info.max_attempts}` : ""}
      </p>
      {info.state === "waiting_for_sources" && waitingOn.length > 0 && (
        <ul className="muted">
          {waitingOn.map((s) => (
            <li key={s.version_id}>
              {s.name}: {s.analysis_state.replace("_", " ")}
            </li>
          ))}
        </ul>
      )}
      {reason && (
        <p className="error" role="alert">
          {reason}
        </p>
      )}
      {info.error_code === "source_failed" && Array.isArray((info.error_detail as { sources?: unknown[] } | null)?.sources) && (
        <p className="muted">A failed file is never skipped: fix or remove it, then try again.</p>
      )}
      <div className="actions">
        {info.can_cancel && !TERMINAL.has(info.state) && (
          <button disabled={busy} onClick={() => void act(() => store.cancel(info.id))}>
            Cancel
          </button>
        )}
        {info.can_retry && (
          <button className="primary" disabled={busy} onClick={() => void act(() => store.retry(info.id))}>
            Try again
          </button>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function CitationMarks(props: { numbers: number[]; onPick: (n: number) => void }) {
  if (props.numbers.length === 0) return null;
  return (
    <span className="cite-marks">
      {props.numbers.map((n) => (
        <button key={n} className="cite" aria-label={`Citation ${n}`} onClick={() => props.onPick(n)}>
          [{n}]
        </button>
      ))}
    </span>
  );
}

function Unsupported() {
  return (
    <span className="unsupported" title="No valid citation backs this statement">
      {" "}
      (no citation)
    </span>
  );
}

function locatorText(c: Citation): string {
  const l = c.locator ?? {};
  const bits: string[] = [];
  if (l.page_start) bits.push(l.page_end && l.page_end !== l.page_start ? `pages ${l.page_start}-${l.page_end}` : `page ${l.page_start}`);
  if (l.figure_number) bits.push(`figure ${l.figure_number}`);
  if (l.heading_path?.length) bits.push(l.heading_path.join(" › "));
  else if (l.section_key) bits.push(l.section_key);
  return bits.join(" · ");
}

const EXTRACTION_TEXT: Record<string, string> = {
  text_layer: "text layer of the file",
  docx: "Word document text",
  plain: "plain text of the file",
  tesseract: "OCR (machine-read, may contain errors)",
  vision_ocr: "vision transcription (machine-read, may contain errors)",
  vision: "figure description (machine-generated)",
};

function originPlace(o: OriginChunk): string {
  const bits: string[] = [];
  if (o.page_start) bits.push(o.page_end && o.page_end !== o.page_start ? `pages ${o.page_start}-${o.page_end}` : `page ${o.page_start}`);
  if (o.heading_path?.length) bits.push(o.heading_path.join(" › "));
  bits.push(`part ${((o.ordinal ?? 0) + 1).toString()}`);
  return bits.join(" · ");
}

/** The original material behind a citation, read from the stored index when the citation is opened. */
function OriginList(props: { origin: CitationOrigin; roomId: string }) {
  const { origin } = props;
  if (origin.source_removed) return <p className="muted">The source was removed. The original text is no longer kept.</p>;
  if (origin.origin.length === 0) return <p className="muted">No original text is recorded for this reference.</p>;
  return (
    <div className="origin-list" aria-label="Original text">
      <p className="small">
        <strong>{origin.derived ? "Original text this summary was made from" : "Original text"}</strong>
        {origin.derived && origin.origin.length > 1 ? ` (${origin.origin.length} passages)` : ""}
      </p>
      {origin.origin.map((o, i) =>
        o.kind === "chunk" ? (
          <figure key={o.chunk_id} className="origin">
            <figcaption className="small muted">
              {o.source_name ?? "a file"}
              {o.version_no ? ` (version ${o.version_no})` : ""} · {originPlace(o)} · {EXTRACTION_TEXT[o.extraction ?? ""] ?? o.extraction}
              {o.integrity === "changed" && <span className="error"> · the stored text differs from what the answer was built on</span>}
              {o.integrity === "unavailable" && <span className="error"> · not available</span>}
            </figcaption>
            {o.text ? <blockquote className="original">{o.text}</blockquote> : <p className="muted">The text of this passage cannot be shown.</p>}
            {o.source_id && o.source_version_id && (
              <a className="small" href={`/rooms/${props.roomId}/sources/${o.source_id}/versions/${o.source_version_id}/download${o.page_start && o.file_kind === "pdf" ? `#page=${o.page_start}` : ""}`}>
                Open the original file{o.page_start && o.file_kind === "pdf" ? ` at page ${o.page_start}` : ""}
              </a>
            )}
          </figure>
        ) : (
          <figure key={`${o.block_id}-${i}`} className="origin">
            <figcaption className="small muted">The open document, as it was when asked (update {o.update_counter ?? "?"})</figcaption>
            {o.text ? <blockquote className="original">{o.text}</blockquote> : <p className="muted">The text of this block cannot be shown.</p>}
          </figure>
        ),
      )}
    </div>
  );
}

export function CitationDetail(props: { api: Api; roomId: string; requestId: string; c: Citation; visible: boolean; onClose: () => void }) {
  const { c } = props;
  const removed = c.source_removed || c.redacted;
  const image = c.figure_chunk_id && !removed && props.visible ? `/rooms/${props.roomId}/ai-requests/${props.requestId}/citations/${c.n}/image` : null;
  const [origin, setOrigin] = useState<CitationOrigin | null>(null);
  const [originFailed, setOriginFailed] = useState(false);
  useEffect(() => {
    setOrigin(null);
    setOriginFailed(false);
    if (removed || !props.visible || !c.origin?.length) return;
    let live = true;
    props.api
      .get<CitationOrigin>(`/rooms/${props.roomId}/ai-requests/${props.requestId}/citations/${c.n}/origin`)
      .then((res) => live && setOrigin(res.body))
      .catch(() => live && setOriginFailed(true));
    return () => {
      live = false;
    };
  }, [props.api, props.roomId, props.requestId, c.n, c.origin?.length, removed, props.visible]);
  return (
    <aside className="cite-detail" aria-label={`Citation ${c.n}`}>
      <p>
        <strong>[{c.n}]</strong> {c.kind === "document" ? "The open document" : c.source_name || "a file"} {locatorText(c) && `· ${locatorText(c)}`}
      </p>
      {c.derived && (
        <p className="derived-note" role="note">
          <strong>Derived text.</strong> {c.derivation?.kind === "section_opening" ? "This is the opening of a section, kept because the model gave no summary of it." : "This is a summary written by the model from the file. It is not text of the file."}
          {c.derivation?.input_truncated ? " The section was longer than the model was shown; the summary covers only the part it saw." : ""}
        </p>
      )}
      {c.machine_generated && <p className="muted">Machine-generated: read by OCR or described by a model. It can contain errors.</p>}
      {c.low_agreement && <p className="error">The two readings of this page disagreed. Treat it as unverified.</p>}
      {c.provenance === "incomplete" && !removed && <p className="error">The original text behind this reference could not be traced. Treat it as unverified.</p>}
      {removed && <p className="muted">The source was removed. Only this reference remains.</p>}
      {!removed && !props.visible && <p className="muted">You may not read the text of this file.</p>}
      {!removed && props.visible && c.excerpt && c.derived && (
        <blockquote className="derived" aria-label="Model-written summary">
          <span className="label">Model-written summary</span> {c.excerpt}
        </blockquote>
      )}
      {!removed && props.visible && c.excerpt && !c.derived && !origin && <blockquote>{c.excerpt}</blockquote>}
      {props.visible && !c.derived && (c.quotes ?? []).map((q) => (
        <blockquote key={q} className="quote">
          “{q}”
        </blockquote>
      ))}
      {origin && <OriginList origin={origin} roomId={props.roomId} />}
      {originFailed && <p className="muted">The original text could not be loaded.</p>}
      {image && <img className="figure-thumb" src={image} alt={`Figure cited as [${c.n}]`} loading="lazy" />}
      <p className="muted small">A citation shows which passage the answer used. It does not prove the passage supports the claim.</p>
      <button onClick={props.onClose}>Close</button>
    </aside>
  );
}

export function AnswerCard(props: { api: Api; roomId: string; result: AiResult; requestId: string }) {
  const { result } = props;
  const [picked, setPicked] = useState<number | null>(null);
  if (result.redacted || !result.answer) {
    return (
      <section className="ai-card answer" aria-label="AI answer">
        <p className="muted">This answer was removed by the room owner.</p>
      </section>
    );
  }
  const cites = new Map(result.citations.map((c) => [c.n, c]));
  const picked_ = picked ? cites.get(picked) : undefined;
  return (
    <section className="ai-card answer" aria-label="AI answer">
      {(result.coverage.notices ?? []).map((n) => (
        <p key={n.code} className="context-notice" role="note">
          <strong>{n.message}</strong> {n.action}
        </p>
      ))}
      {result.answer.blocks.map((b, i) => (
        <Block key={i} block={b} onPick={setPicked} />
      ))}
      {result.answer.evidence_gaps.length > 0 && (
        <div className="gaps">
          <strong>Not found in the sources</strong>
          <ul>
            {result.answer.evidence_gaps.map((g) => (
              <li key={g}>{g}</li>
            ))}
          </ul>
        </div>
      )}
      <Coverage result={result} />
      {picked_ && (
        <CitationDetail
          api={props.api}
          roomId={props.roomId}
          requestId={props.requestId}
          c={picked_}
          visible={result.citation_text_visible}
          onClose={() => setPicked(null)}
        />
      )}
      <p className="muted small">
        AI answers can be wrong. Citations are references to the passages used, not proof that they support each claim.
      </p>
    </section>
  );
}

function Block(props: { block: AnswerBlock; onPick: (n: number) => void }) {
  const b = props.block;
  if (b.type === "paragraph") {
    return (
      <p>
        {b.text}
        <CitationMarks numbers={b.citations} onPick={props.onPick} />
        {b.unsupported && b.note !== "no_evidence" && <Unsupported />}
      </p>
    );
  }
  if (b.type === "bullet_list") {
    return (
      <ul>
        {b.items.map((it, i) => (
          <li key={i}>
            {it.text}
            <CitationMarks numbers={it.citations} onPick={props.onPick} />
            {it.unsupported && <Unsupported />}
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div>
      <SafeMermaid source={b.mermaid} title={b.title} serverNote={b.render_status === "failed" ? b.render_error : null} />
      {b.assumptions && b.assumptions.length > 0 && (
        <div className="assumptions" role="note" aria-label="Assumptions in this diagram">
          <strong>Assumed, not stated in the sources:</strong>
          <ul>
            {b.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </div>
      )}
      <CitationMarks numbers={b.citations} onPick={props.onPick} />
    </div>
  );
}

function Coverage({ result }: { result: AiResult }) {
  const cov = result.coverage;
  const lines: string[] = [];
  for (const s of cov.sources ?? []) {
    const how = s.mode === "full" ? "read in full" : s.mode === "sampled" ? "sampled, not read in full" : "searched for relevant passages";
    lines.push(`${s.source_name}: ${how} (${s.sections_read} of ${s.sections_total} sections${s.figures_total ? `, ${s.figures_examined} of ${s.figures_total} figures` : ""})`);
  }
  if (cov.document) {
    const d = cov.document;
    if (d.mode === "target_plus_context" || d.mode === "target_plus_partial_context") {
      const c = d.context;
      lines.push(
        `Open document (as saved on the server at update ${d.captured_update_counter}): the AI could change only your target (${d.blocks_included} blocks) and read the rest as context` +
          (c ? (c.complete ? `: all ${c.blocks_total} blocks` : `: only ${c.blocks_shown} of ${c.blocks_total} blocks (${c.omitted_blocks} not shown)`) : ""),
      );
    } else {
      lines.push(`Open document (as saved on the server at update ${d.captured_update_counter}): ${d.blocks_included} of ${d.blocks_total} blocks, ${d.mode}`);
    }
  }
  if (cov.chat_messages_included) lines.push(`${cov.chat_messages_included} chat message(s) included`);
  if (lines.length === 0) return null;
  return (
    <details className="coverage">
      <summary>What this answer looked at</summary>
      <ul>
        {lines.map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
      <p className="muted small">Edits still unsynced in someone's browser are not visible to the AI.</p>
    </details>
  );
}
