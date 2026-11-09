/**
 * The two chat cards: the REQUEST card (what the person who asked sees while it runs) and the ANSWER
 * card (what every member sees once it is published).
 *
 * Text is rendered as TEXT. Nothing the model wrote is ever turned into HTML or a link; a diagram goes
 * through SafeMermaid. A citation is a reference, and the card says so: it shows which passage the
 * answer used, not that the passage proves the claim. Machine-read text (OCR, vision, figure
 * descriptions) is labelled as machine-generated. A claim that lost its citation is flagged.
 */
import { useState } from "react";
import type { Api } from "../api";
import { ApiError } from "../api";
import { SafeMermaid } from "./SafeMermaid";
import type { AiStore } from "./store";
import { TERMINAL } from "./store";
import { type AiResult, type AnswerBlock, type Citation, ERROR_TEXT, type RequestInfo } from "./types";

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

function CitationDetail(props: { api: Api; roomId: string; requestId: string; c: Citation; visible: boolean; onClose: () => void }) {
  const { c } = props;
  const removed = c.source_removed || c.redacted;
  const image = c.figure_chunk_id && !removed && props.visible ? `/rooms/${props.roomId}/ai-requests/${props.requestId}/citations/${c.n}/image` : null;
  return (
    <aside className="cite-detail" aria-label={`Citation ${c.n}`}>
      <p>
        <strong>[{c.n}]</strong> {c.kind === "document" ? "The open document" : c.source_name || "a file"} {locatorText(c) && `· ${locatorText(c)}`}
      </p>
      {c.machine_generated && <p className="muted">Machine-generated: read by OCR or described by a model. It can contain errors.</p>}
      {c.low_agreement && <p className="error">The two readings of this page disagreed. Treat it as unverified.</p>}
      {removed && <p className="muted">The source was removed. Only this reference remains.</p>}
      {!removed && !props.visible && <p className="muted">You may not read the text of this file.</p>}
      {!removed && props.visible && c.excerpt && <blockquote>{c.excerpt}</blockquote>}
      {props.visible && (c.quotes ?? []).map((q) => (
        <blockquote key={q} className="quote">
          “{q}”
        </blockquote>
      ))}
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
    lines.push(`Open document (as saved on the server at update ${d.captured_update_counter}): ${d.blocks_included} of ${d.blocks_total} blocks, ${d.mode}`);
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
