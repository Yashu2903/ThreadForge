/**
 * The review card of an AI proposal: an edit to a document, or a draft of a new one.
 *
 * What it shows: the target (the document, named even if it is not the one open now), a summary, the
 * sources used (citation marks, each opening the cited passage), and a PREVIEW: for every change the
 * blocks before and after, with a word-level diff for text changes, rendered diagrams included, and the
 * model's reason. Previewing reads a stored copy; it never touches the live document, and generating a
 * proposal never changed it either. Everything the model wrote is shown as text (a link is shown, never
 * made clickable here); a diagram goes through SafeMermaid.
 *
 * ACTIONS (the server decides who may do what and returns `can_*`; these only ask):
 *   Apply     changes the shared document for everyone connected, once, atomically. It asks first, says how many
 *             other people are connected, and says that edits they have not synced yet can merge in oddly. The
 *             result distinguishes "sent to the editors" from "saved": both are reported.
 *   Reject    changes nothing.
 *   Regenerate (after a conflict) asks again from the document's CURRENT text; the old preview stays readable.
 *   Publish   (draft, owner only) creates the new document and makes it the room's active one; it asks first.
 */
import { Fragment, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { Api } from "../api";
import { ApiError } from "../api";
import { CitationDetail } from "./Cards";
import { SafeMermaid } from "./SafeMermaid";
import type { AiStore } from "./store";
import { type ApplyReply, ERROR_TEXT, type PMNode, type ProposalChange, type ProposalInfo, type ProposalState } from "./types";

const STATE_TEXT: Record<ProposalState, string> = {
  pending: "Waiting for review",
  applying: "Being applied",
  applied: "Applied",
  rejected: "Rejected",
  conflicted: "The document changed",
  expired: "Expired",
  superseded: "Replaced by a newer proposal",
};

export function ProposalCard(props: {
  api: Api;
  store: AiStore;
  roomId: string;
  requestId: string;
  proposalId: string;
  fallback: string;
  /** the document open in the editor now, and how many other people are connected to it */
  openDocumentId?: string | null;
  peers?: () => number | null;
  isOwner?: boolean;
  userId?: string;
}) {
  const { store, proposalId } = props;
  useSyncExternalStore(store.subscribe, store.getSnapshot); // re-render when a proposal arrives or changes
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<number | null>(null);
  useEffect(() => {
    void store.ensureProposal(proposalId);
  }, [store, proposalId]);
  const proposal = store.proposals.get(proposalId);
  if (!proposal) return <section className="ai-card proposal" aria-label="AI proposal"><p className="muted">Loading the proposal…</p></section>;
  if (proposal === "forbidden") {
    // viewers and editors without write access see only the ordinary chat message
    return <span>{props.fallback}</span>;
  }
  if (proposal === "missing") return <span>{props.fallback}</span>;
  const p: ProposalInfo = proposal;
  const cites = new Map(p.citations.map((c) => [c.n, c]));
  const picked_ = picked ? cites.get(picked) : undefined;
  const title = p.kind === "edit" ? `Proposed edit to “${p.document_title ?? "a document"}”` : `Draft of a new document: “${p.document_title ?? p.preview.title ?? "Untitled"}”`;
  const wiped = p.operation_count === 0 && !p.preview.changes && !p.preview.document;

  return (
    <section className="ai-card proposal" aria-label="AI proposal" data-state={p.state} data-kind={p.kind}>
      <header>
        <strong>{title}</strong> <span className={`badge state-${p.state}`}>{STATE_TEXT[p.state]}</span>
      </header>
      {p.summary && <p>{p.summary}</p>}
      <p className="muted small">{targetLine(p)}</p>
      <p className="notice small" role="note">
        {p.kind === "edit"
          ? "This is a preview. Nothing has changed in the document."
          : "This is a draft. It is not a document yet and nothing in the room has changed."}
      </p>
      {wiped ? (
        <p className="muted">The content of this proposal was removed.</p>
      ) : (
        <>
          <button type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)} data-testid="preview-toggle">
            {open ? "Hide preview" : "Preview"}
          </button>
          {open && (p.kind === "edit" ? <EditPreview p={p} onPick={setPicked} /> : <DraftPreview p={p} onPick={setPicked} />)}
        </>
      )}
      <Actions p={p} store={store} openDocumentId={props.openDocumentId ?? null} peers={props.peers} />
      {picked_ && (
        <CitationDetail api={props.api} roomId={props.roomId} requestId={props.requestId} c={picked_} visible={p.citation_text_visible} onClose={() => setPicked(null)} />
      )}
      <p className="muted small">
        AI proposals can be wrong. Citations are references to the passages used, not proof that they support each change. Expires{" "}
        {new Date(p.expires_at).toLocaleDateString()}.
      </p>
    </section>
  );
}

function Actions(props: { p: ProposalInfo; store: AiStore; openDocumentId: string | null; peers?: () => number | null }) {
  const { p, store } = props;
  const [confirm, setConfirm] = useState<"apply" | "publish" | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const publishId = useRef(crypto.randomUUID()); // one id per card: a retry of Publish cannot create a second document
  const [peerCount, setPeerCount] = useState<number | null>(null);

  async function run(fn: () => Promise<string>) {
    setBusy(true);
    setMessage(null);
    try {
      setMessage({ kind: "ok", text: await fn() });
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "";
      setMessage({ kind: "error", text: ERROR_TEXT[code] ?? (err instanceof ApiError ? err.message : "Could not reach the server. Nothing was changed.") });
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  }

  const apply = () =>
    run(async () => {
      const r: ApplyReply = await store.applyProposal(p.id);
      const others = r.connected_users > 0 ? ` ${r.connected_users} other ${r.connected_users === 1 ? "person was" : "people were"} connected and saw it appear.` : "";
      if (r.already) return "This proposal had already been applied; nothing more was changed.";
      return `Applied. The change reached the connected editors${r.synced ? "" : " (not yet)"} and is saved${r.durable ? "" : " (not yet: it will be finished automatically)"}.${others}`;
    });

  if (p.state === "applying") {
    return (
      <p className="muted" role="status">
        Being applied: the change has reached the editors and is being saved. This finishes by itself.
      </p>
    );
  }
  if (p.state === "applied") {
    return (
      <p role="status" data-testid="applied">
        {p.kind === "draft" ? "Published as a document." : "Applied to the document."}
        {message && message.kind === "ok" ? ` ${message.text}` : ""}
      </p>
    );
  }
  const buttons: React.ReactNode[] = [];
  if (p.state === "pending" && p.can_apply) {
    buttons.push(
      <button
        key="apply"
        type="button"
        className="primary"
        disabled={busy}
        onClick={() => (setPeerCount(props.p.document_id && props.p.document_id === props.openDocumentId ? (props.peers?.() ?? null) : null), setConfirm("apply"))}
      >
        Apply
      </button>,
    );
  }
  if (p.state === "pending" && p.can_publish) {
    buttons.push(
      <button key="publish" type="button" className="primary" disabled={busy} onClick={() => setConfirm("publish")}>
        Publish as a new document
      </button>,
    );
  }
  if (p.state === "pending" && p.can_reject) {
    buttons.push(
      <button key="reject" type="button" disabled={busy} onClick={() => void run(async () => (await store.rejectProposal(p.id), "Rejected. Nothing was changed."))}>
        Reject
      </button>,
    );
  }
  if (p.state === "conflicted" && p.can_regenerate) {
    buttons.push(
      <button key="regen" type="button" className="primary" disabled={busy} onClick={() => void run(async () => (await store.regenerateProposal(p.id), "Asking again from the current text…"))}>
        Regenerate from the current text
      </button>,
    );
  }
  return (
    <div className="proposal-actions">
      {p.state === "conflicted" && (
        <p className="error" role="alert">
          The document changed after this was proposed, so nothing was changed.
        </p>
      )}
      {p.state === "pending" && p.kind === "draft" && !p.can_publish && <p className="muted small">Only the room owner can publish this draft.</p>}
      {buttons.length > 0 && <div className="actions">{buttons}</div>}
      {confirm === "apply" && (
        <div role="alertdialog" aria-label="Confirm apply" className="confirm">
          <p>
            This changes “{p.document_title ?? "the document"}” for everyone, now.
            {peerCount !== null && peerCount > 0
              ? ` ${peerCount} other ${peerCount === 1 ? "person is" : "people are"} connected to it and will see the change appear.`
              : ""}
          </p>
          <p className="muted small">
            A check protects the blocks this proposal targets. It cannot see edits still sitting unsent in someone's browser or made offline: those merge in later and can leave mixed text where a block was replaced.
          </p>
          <button type="button" className="primary" disabled={busy} onClick={() => void apply()}>
            Apply now
          </button>
          <button type="button" disabled={busy} onClick={() => setConfirm(null)}>
            Cancel
          </button>
        </div>
      )}
      {confirm === "publish" && (
        <div role="alertdialog" aria-label="Confirm publish" className="confirm">
          <p>
            This creates the document “{p.document_title ?? "Untitled"}” and <strong>makes it the room's active document</strong>, the one everyone sees.
          </p>
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => void run(async () => (await store.publishDraft(p.id, publishId.current), "Published. It is now the active document."))}
          >
            Create the document
          </button>
          <button type="button" disabled={busy} onClick={() => setConfirm(null)}>
            Cancel
          </button>
        </div>
      )}
      {message && (
        <p role={message.kind === "error" ? "alert" : "status"} className={message.kind === "error" ? "error" : ""}>
          {message.text}
        </p>
      )}
    </div>
  );
}

function targetLine(p: ProposalInfo): string {
  if (p.kind === "draft") return "Target: a new document in this room (the owner decides whether to publish it).";
  const s = p.scope;
  const bits: string[] = [];
  if (s.block_ids?.length) bits.push(`${s.block_ids.length} selected block${s.block_ids.length === 1 ? "" : "s"}`);
  if (s.ranges?.length) bits.push(`${s.ranges.length} selected text range${s.ranges.length === 1 ? "" : "s"}`);
  if (s.insert_at) bits.push(`the ${s.insert_at} of the document`);
  return `Target: “${p.document_title ?? "a document"}”${bits.length ? ` · ${bits.join(", ")}` : ""} · based on the document as saved at update ${p.captured_counter ?? "?"}.`;
}

// ---- preview ----------------------------------------------------------------------------------------------

function Cites(props: { numbers: number[]; onPick: (n: number) => void }) {
  if (props.numbers.length === 0) return null;
  return (
    <span className="cite-marks">
      {props.numbers.map((n) => (
        <button key={n} type="button" className="cite" aria-label={`Citation ${n}`} onClick={() => props.onPick(n)}>
          [{n}]
        </button>
      ))}
    </span>
  );
}

function EditPreview({ p, onPick }: { p: ProposalInfo; onPick: (n: number) => void }) {
  const changes = p.preview.changes ?? [];
  return (
    <div className="proposal-preview" aria-label="Preview of the changes">
      {changes.map((c, i) => (
        <ChangeView key={`${c.op}-${i}`} c={c} index={i} onPick={onPick} />
      ))}
    </div>
  );
}

function kindLabel(c: ProposalChange): string {
  const first = c.before[0] ?? c.after[0];
  const type = first?.type === "heading" ? "heading" : first?.type === "diagram" ? "diagram" : "paragraph";
  switch (c.kind) {
    case "replace_block_text":
      return `Replace the text of a ${type}`;
    case "replace_range":
      return "Replace part of a paragraph";
    case "insert_blocks_after":
      return `${c.anchor === "start" ? "Insert at the start of the document" : "Insert after the selected block"}: ${c.after.length} new block${c.after.length === 1 ? "" : "s"}`;
    case "delete_blocks":
      return `Delete ${c.before.length} block${c.before.length === 1 ? "" : "s"}`;
  }
}

function ChangeView({ c, index, onPick }: { c: ProposalChange; index: number; onPick: (n: number) => void }) {
  const textual = c.kind === "replace_block_text" || c.kind === "replace_range";
  const diff = textual && c.before[0] && c.after[0] ? diffWords(plain(c.before[0]), plain(c.after[0])) : null;
  return (
    <article className="change" aria-label={`Change ${index + 1}`}>
      <h4>
        {index + 1}. {kindLabel(c)}
      </h4>
      {c.reason && <p className="muted small">Why: {c.reason}</p>}
      <p className="small">
        <Cites numbers={c.citations} onPick={onPick} />
        {c.unsupported && <span className="unsupported"> (no citation)</span>}
      </p>
      {diff && (
        <p className="diff" aria-label="Changed words">
          {diff.map((d, i) =>
            d.t === "same" ? <Fragment key={i}>{d.s}</Fragment> : d.t === "del" ? <del key={i}>{d.s}</del> : <ins key={i}>{d.s}</ins>,
          )}
        </p>
      )}
      <div className="before-after">
        {c.before.length > 0 && (
          <div className="before" aria-label="Before">
            <span className="label">Before</span>
            <Blocks nodes={c.before} />
          </div>
        )}
        {c.after.length > 0 && (
          <div className="after" aria-label="After">
            <span className="label">After</span>
            <Blocks nodes={c.after} />
          </div>
        )}
      </div>
    </article>
  );
}

function DraftPreview({ p, onPick }: { p: ProposalInfo; onPick: (n: number) => void }) {
  const nodes = p.preview.document?.content ?? [];
  const cites = p.preview.block_citations ?? [];
  return (
    <div className="proposal-preview draft" aria-label="Preview of the draft">
      <h3>{p.preview.title ?? p.document_title}</h3>
      {nodes.map((n, i) => (
        <Fragment key={i}>
          <Blocks nodes={[n]} />
          {cites[i] && cites[i]!.length > 0 && <Cites numbers={cites[i]!} onPick={onPick} />}
        </Fragment>
      ))}
    </div>
  );
}

// ---- rendering stored nodes as text --------------------------------------------------------------------------

export function plain(node: PMNode): string {
  if (node.type === "text") return node.text ?? "";
  if (node.type === "hardBreak") return "\n";
  return (node.content ?? []).map(plain).join("");
}

function Inline({ nodes }: { nodes: PMNode[] | undefined }) {
  return (
    <>
      {(nodes ?? []).map((n, i) => {
        if (n.type === "hardBreak") return <br key={i} />;
        if (n.type === "image") return <span key={i} className="muted">[image]</span>;
        let el: React.ReactNode = n.text ?? "";
        for (const m of n.marks ?? []) {
          if (m.type === "bold") el = <strong>{el}</strong>;
          else if (m.type === "italic") el = <em>{el}</em>;
          else if (m.type === "underline") el = <u>{el}</u>;
          else if (m.type === "strike") el = <s>{el}</s>;
          else if (m.type === "link") el = <span className="link" title={String(m.attrs?.href ?? "")}>{el}</span>;
        }
        return <Fragment key={i}>{el}</Fragment>;
      })}
    </>
  );
}

export function Blocks({ nodes }: { nodes: PMNode[] }) {
  return (
    <>
      {nodes.map((n, i) => (
        <Block key={i} n={n} />
      ))}
    </>
  );
}

function Block({ n }: { n: PMNode }) {
  switch (n.type) {
    case "paragraph":
      return <p><Inline nodes={n.content} /></p>;
    case "heading": {
      const level = Math.min(6, 3 + Number(n.attrs?.level ?? 1));
      const Tag = `h${level}` as "h4";
      return <Tag><Inline nodes={n.content} /></Tag>;
    }
    case "blockquote":
      return <blockquote><Blocks nodes={n.content ?? []} /></blockquote>;
    case "bulletList":
    case "orderedList": {
      const Tag = n.type === "bulletList" ? "ul" : "ol";
      return (
        <Tag>
          {(n.content ?? []).map((li, i) => (
            <li key={i}>
              {(li.content ?? []).map((c, j) => (c.type === "paragraph" ? <span key={j}><Inline nodes={c.content} /></span> : <Block key={j} n={c} />))}
            </li>
          ))}
        </Tag>
      );
    }
    case "table":
      return (
        <table>
          <tbody>
            {(n.content ?? []).map((row, r) => (
              <tr key={r}>
                {(row.content ?? []).map((cell, c) => {
                  const Cell = cell.type === "tableHeader" ? "th" : "td";
                  return <Cell key={c}>{(cell.content ?? []).map((p, k) => <Fragment key={k}><Inline nodes={p.content} /> </Fragment>)}</Cell>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      );
    case "diagram":
      return <SafeMermaid source={plain(n)} title="" />;
    default:
      return <p className="muted">[{n.type ?? "block"}]</p>;
  }
}

// ---- a small word diff ---------------------------------------------------------------------------------------------

export type DiffPart = { t: "same" | "del" | "add"; s: string };

/** Longest-common-subsequence diff over words and whitespace; skipped (null) for very long texts. */
export function diffWords(a: string, b: string): DiffPart[] | null {
  const x = a.split(/(\s+)/).filter((s) => s !== "");
  const y = b.split(/(\s+)/).filter((s) => s !== "");
  if (x.length * y.length > 160_000) return null;
  const dp: number[][] = Array.from({ length: x.length + 1 }, () => new Array<number>(y.length + 1).fill(0));
  for (let i = x.length - 1; i >= 0; i--) {
    for (let j = y.length - 1; j >= 0; j--) {
      dp[i]![j] = x[i] === y[j] ? dp[i + 1]![j + 1]! + 1 : Math.max(dp[i + 1]![j]!, dp[i]![j + 1]!);
    }
  }
  const out: DiffPart[] = [];
  const push = (t: DiffPart["t"], s: string) => {
    const last = out[out.length - 1];
    if (last && last.t === t) last.s += s;
    else out.push({ t, s });
  };
  let i = 0;
  let j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) (push("same", x[i]!), i++, j++);
    else if (dp[i + 1]![j]! >= dp[i]![j + 1]!) (push("del", x[i]!), i++);
    else (push("add", y[j]!), j++);
  }
  while (i < x.length) push("del", x[i++]!);
  while (j < y.length) push("add", y[j++]!);
  return out;
}
