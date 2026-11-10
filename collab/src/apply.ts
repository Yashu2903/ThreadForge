/**
 * Applying a reviewed AI proposal to the LIVE shared document. The authoritative path.
 *
 * WHY THIS IS SAFE TO DO IN ONE PROCESS (single collaboration instance; no epochs, no freezing): this service
 * holds the live Y.Doc and handles every update to it on one thread. A synchronous Yjs transaction is therefore
 * atomic against every update the server has ALREADY RECEIVED. What it cannot see is an edit still sitting in
 * a browser (unsent, or made offline): if it is inside a block we replace or delete it is dropped when it arrives (docs/ai.md 19.6).
 *
 *   POST /internal/ai/apply-proposal {proposal_id, document_id, user_id}      (secret-protected, private network)
 *
 *   1  lease      this instance still holds the lease (a stale instance refuses: the token is sent with every
 *                 internal write and the API checks it inside the transaction)
 *   2  authority  the API rechecks, NOW: can_write for the applying user; the proposal is pending, unexpired,
 *                 an edit, of this document and room; and moves it pending -> applying with a compare-and-set,
 *                 so two applies cannot both proceed
 *   3  quiet      if a PERSON's edit reached the document within AI_APPLY_QUIET_SECONDS, wait (up to
 *                 AI_APPLY_MAX_WAIT_SECONDS) for quiet, else `document_busy` (retryable). Document level, not
 *                 block level.
 *   4  check      in memory, before anything changes: read the live document, recompute every target block's
 *                 hash and compare it with the hash the proposal was based on; run the SAME engine the worker
 *                 used to build the candidate; validate the whole result; rehearse the Yjs change on a COPY and
 *                 require the identical document. Any mismatch -> `conflicted`, nothing changed.
 *   5  commit     ONE doc.transact with its own origin (never a person's, so the editors' UndoManager does not
 *                 track it): all changes together, plus an idempotency MARKER in the Y.Map `ai_applications`
 *                 keyed by proposal id. An existing marker means "already applied": no change, same outcome.
 *   6  durable    store NOW (not debounced), with the application receipt in the same PostgreSQL transaction as
 *                 the Yjs bytes; only after that commits, mark the proposal applied and answer.
 *   7  recovery   a sweeper (at start and every minute) finds proposals stuck in `applying`: marker present ->
 *                 record and finish; marker absent -> back to pending.
 *
 * Between 4 and 5 nothing may happen to the document: the state vector is read before the checks and compared
 * with the one just before the transaction; any update that slipped in (hashing is asynchronous) restarts the
 * check, at most three times. Logs carry ids, counts and codes, never document text.
 */
import { createHash, randomUUID } from "node:crypto";
import {
  type OpScope,
  type PMJson,
  YJS_FIELD,
  applyOperations,
  blockHash,
  canonicalStringify,
  canonicalize,
  contentHash,
} from "@threadforge/editor-schema";
import { yDocToProsemirrorJSON } from "@tiptap/y-tiptap";
import * as Y from "yjs";
import { ApiError, type BeginApply, type InternalApi } from "./api.js";
import type { CollabConfig } from "./config.js";
import type { Logger } from "./log.js";
import type { CollabServer } from "./server.js";
import { AI_ORIGIN_SOURCE, ApplyError, MARKER_MAP, applyChangesToYjs } from "./yapply.js";

export interface Reply {
  status: number;
  body: unknown;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_CHECK_ATTEMPTS = 3;
const sha256 = (text: string) => createHash("sha256").update(text).digest("hex");

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** every node that has an id, by id */
function indexById(doc: PMJson): Map<string, PMJson> {
  const out = new Map<string, PMJson>();
  const walk = (node: PMJson) => {
    const id = node.attrs?.id;
    if (typeof id === "string") out.set(id, node);
    (node.content ?? []).forEach(walk);
  };
  walk(doc);
  return out;
}

export class ProposalApplier {
  private readonly inflight = new Map<string, Promise<Reply>>();
  private readonly chains = new Map<string, Promise<unknown>>();
  private sweepTimer: NodeJS.Timeout | null = null;
  private sweeping = false;

  constructor(
    private readonly server: CollabServer,
    private readonly api: InternalApi,
    private readonly config: CollabConfig,
    private readonly log: Logger,
  ) {}

  // ---- the request ------------------------------------------------------------------------------------------

  async apply(input: { proposal_id?: unknown; document_id?: unknown; user_id?: unknown }): Promise<Reply> {
    const { proposal_id: proposalId, document_id: documentId, user_id: userId } = input;
    if (
      typeof proposalId !== "string" || !UUID.test(proposalId) ||
      typeof documentId !== "string" || !UUID.test(documentId) ||
      typeof userId !== "string" || !UUID.test(userId)
    ) {
      return { status: 422, body: { error: "invalid_request" } };
    }
    // the same proposal applied twice at once: the second caller gets the first one's answer
    const running = this.inflight.get(proposalId);
    if (running) return running;
    const task = this.serialized(documentId, () => this.applyLocked(proposalId, documentId, userId));
    this.inflight.set(proposalId, task);
    try {
      return await task;
    } finally {
      this.inflight.delete(proposalId);
    }
  }

  /** one apply at a time per document (the order of two different proposals is first come, first served) */
  private serialized<T>(documentId: string, fn: () => Promise<T>): Promise<T> {
    const previous = this.chains.get(documentId) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(fn);
    this.chains.set(documentId, next);
    void next.finally(() => {
      if (this.chains.get(documentId) === next) this.chains.delete(documentId);
    }).catch(() => undefined);
    return next;
  }

  private fail(error: unknown): Reply {
    if (error instanceof ApiError) {
      if (error.code === "stale_lease") return { status: 409, body: { error: "stale_lease" } };
      if (error.status >= 400 && error.status < 500) return { status: error.status, body: { error: error.code, ...error.extra } };
    }
    return { status: 503, body: { error: "unavailable" } };
  }

  private async applyLocked(proposalId: string, documentId: string, userId: string): Promise<Reply> {
    // 1. the lease
    let token: number;
    try {
      token = this.server.lease.token;
    } catch {
      return { status: 409, body: { error: "stale_lease" } };
    }
    // 2. authority and compare-and-set (a refusal changes nothing)
    let begin: BeginApply;
    try {
      begin = await this.api.beginApply(proposalId, token, { user_id: userId, document_id: documentId });
    } catch (error) {
      return this.fail(error);
    }
    if (begin.already_applied) {
      return { status: 200, body: { state: "applied", already: true, synced: true, durable: true, counter: begin.resulting_counter ?? null } };
    }
    const name = `doc:${documentId}`;
    let mutated = false;
    try {
      return await this.server.withDocument(documentId, { source: AI_ORIGIN_SOURCE, requester_id: userId, proposal_id: proposalId }, async ({ document, direct }) => {
        // 3. quiet
        if (!(await this.waitForQuiet(name))) {
          await this.release(proposalId, token, "document_busy");
          return { status: 409, body: { error: "document_busy", retryable: true } };
        }
        // 4. check, 5. commit
        let result: ReturnType<typeof applyOperations> | null = null;
        let marked = false;
        for (let attempt = 1; attempt <= MAX_CHECK_ATTEMPTS && !marked && result === null; attempt += 1) {
          const before = Y.encodeStateVector(document);
          if (document.getMap(MARKER_MAP).has(proposalId)) {
            marked = true; // already applied (a crash after the change, or a repeat): no change, same outcome
            break;
          }
          const current = canonicalize(yDocToProsemirrorJSON(document, YJS_FIELD) as PMJson);
          const found = indexById(current);
          for (const [id, expected] of Object.entries(begin.base_hashes)) {
            const node = found.get(id);
            if (!node) return await this.conflict(proposalId, token, userId, "block_missing");
            if ((await blockHash(node)) !== expected) return await this.conflict(proposalId, token, userId, "block_changed");
          }
          const scope: OpScope = {
            wholeIds: new Set(begin.scope.whole_ids ?? []),
            ranges: begin.scope.ranges ?? [],
            anchors: begin.scope.anchors ?? [],
          };
          const candidate = applyOperations(current, begin.operations, { scope, newId: randomUUID, limits: this.config.limits });
          if (!candidate.ok) {
            return await this.conflict(proposalId, token, userId, `candidate_${candidate.errors[0]?.code ?? "invalid"}`);
          }
          // rehearse on a copy: the real Yjs result must be the validated document, byte for byte of meaning
          const copy = new Y.Doc();
          try {
            Y.applyUpdate(copy, Y.encodeStateAsUpdate(document));
            copy.transact(() => applyChangesToYjs(copy, candidate.changes));
            const rehearsal = canonicalize(yDocToProsemirrorJSON(copy, YJS_FIELD) as PMJson);
            if ((await contentHash(rehearsal)) !== (await contentHash(candidate.doc))) {
              this.log.error("proposal rehearsal mismatch", { proposal: proposalId, document: documentId });
              await this.release(proposalId, token, "apply_mismatch");
              return { status: 500, body: { error: "apply_mismatch" } };
            }
          } catch (error) {
            if (error instanceof ApplyError) return await this.conflict(proposalId, token, userId, error.code);
            throw error;
          } finally {
            copy.destroy();
          }
          // nothing may have reached the document while the (asynchronous) checks ran
          if (!sameBytes(before, Y.encodeStateVector(document))) continue;
          const receiptHash = sha256(
            canonicalStringify({ proposal: proposalId, operations: sha256(canonicalStringify(begin.operations)), result: await contentHash(candidate.doc) }),
          );
          if (!sameBytes(before, Y.encodeStateVector(document))) continue; // (hashing awaited again)
          // ---- the atomic step: synchronous, one transaction, its own origin ----------------------------------
          void direct.transact((live) => {
            applyChangesToYjs(live, candidate.changes);
            live.getMap(MARKER_MAP).set(proposalId, { user: userId, at: new Date().toISOString(), receipt: receiptHash });
          });
          mutated = true;
          result = candidate;
        }
        if (!marked && result === null) {
          await this.release(proposalId, token, "document_busy");
          return { status: 409, body: { error: "document_busy", retryable: true } };
        }
        // 6. durable: store NOW, with the receipt in the same database transaction as the bytes
        const marker = document.getMap(MARKER_MAP).get(proposalId) as { receipt?: string } | undefined;
        const receiptHash = marker?.receipt ?? "";
        const stored = await this.server.storeNow(name, document, { proposalId, userId, receiptHash });
        if (!stored.durable) throw new ApiError(503, "store_failed"); // in memory and broadcast, not saved yet: the sweeper finishes it
        const done = await this.api.finishApply(proposalId, token, {
          user_id: userId,
          resulting_counter: stored.counter,
          receipt_hash: receiptHash,
        });
        this.log.info("proposal applied", { proposal: proposalId, document: documentId, counter: stored.counter, already: marked });
        return {
          status: 200,
          body: {
            state: done.state,
            already: marked && !mutated,
            // "synced": every connected editor already has it (the change was broadcast); "durable": it is in
            // PostgreSQL, with its receipt. Both are true here; they are separate facts and reported separately.
            synced: true,
            durable: true,
            counter: stored.counter,
            connected_users: this.server.connectedUsers(name).filter((u) => u.userId !== userId).length,
          },
        };
      });
    } catch (error) {
      if (!mutated) {
        await this.release(proposalId, token, "apply_failed").catch(() => undefined);
      } else {
        // the change was made and broadcast but could not be made durable yet: the proposal stays `applying`,
        // connected editors hold the change (and the marker), and the sweeper finishes it
        this.log.error("proposal applied in memory but not durable yet", { proposal: proposalId, document: documentId });
      }
      return this.fail(error);
    }
  }

  private async waitForQuiet(name: string): Promise<boolean> {
    const quiet = this.config.aiApplyQuietMs;
    const deadline = Date.now() + this.config.aiApplyMaxWaitMs;
    for (;;) {
      const since = this.server.msSinceHumanUpdate(name);
      if (since >= quiet) return true;
      const wait = quiet - since;
      if (Date.now() + wait > deadline) return false;
      await new Promise((resolve) => setTimeout(resolve, Math.min(wait + 5, 500)));
    }
  }

  private async conflict(proposalId: string, token: number, userId: string, code: string): Promise<Reply> {
    this.log.info("proposal conflicted", { proposal: proposalId, code });
    try {
      await this.api.markConflicted(proposalId, token, { user_id: userId, code });
    } catch (error) {
      return this.fail(error);
    }
    return { status: 409, body: { error: "conflicted", code } };
  }

  private async release(proposalId: string, token: number, reason: string): Promise<void> {
    try {
      await this.api.releaseApply(proposalId, token, reason);
    } catch (error) {
      this.log.warn("could not return a proposal to pending", { proposal: proposalId, code: error instanceof ApiError ? error.code : "error" });
    }
  }

  // ---- recovery ----------------------------------------------------------------------------------------------

  startSweeper(): void {
    if (this.sweepTimer || this.config.aiApplySweepMs <= 0) return;
    void this.sweep();
    this.sweepTimer = setInterval(() => void this.sweep(), this.config.aiApplySweepMs);
    this.sweepTimer.unref?.();
  }

  stopSweeper(): void {
    if (this.sweepTimer) clearInterval(this.sweepTimer);
    this.sweepTimer = null;
  }

  /**
   * Proposals stuck in `applying` (a crash, a store that failed after the change): load the document and
   * look for the marker. Present -> the change IS in the document; store it, record the receipt, mark applied.
   * Absent -> nothing was applied (or the change never became durable and no client held it): back to pending.
   * Returns how many were resolved.
   */
  async sweep(olderThanMs = this.config.aiApplyStuckAfterMs): Promise<number> {
    if (this.sweeping) return 0;
    this.sweeping = true;
    let resolved = 0;
    try {
      let token: number;
      try {
        token = this.server.lease.token;
      } catch {
        return 0;
      }
      const stuck = await this.api.stuckProposals(Math.floor(olderThanMs / 1000));
      for (const item of stuck) {
        if (this.inflight.has(item.proposal_id)) continue;
        try {
          await this.serialized(item.document_id, () => this.recover(item.proposal_id, item.document_id, item.applying_by, token));
          resolved += 1;
        } catch (error) {
          this.log.warn("sweep could not resolve a proposal", { proposal: item.proposal_id, code: error instanceof ApiError ? error.code : "error" });
        }
      }
    } catch (error) {
      this.log.warn("proposal sweep failed", { code: error instanceof ApiError ? error.code : "error" });
    } finally {
      this.sweeping = false;
    }
    return resolved;
  }

  private async recover(proposalId: string, documentId: string, userId: string, token: number): Promise<void> {
    const name = `doc:${documentId}`;
    await this.server.withDocument(documentId, { source: AI_ORIGIN_SOURCE, requester_id: userId, proposal_id: proposalId }, async ({ document }) => {
      const marker = document.getMap(MARKER_MAP).get(proposalId) as { receipt?: string } | undefined;
      if (!marker) {
        await this.api.releaseApply(proposalId, token, "recovered_not_applied");
        this.log.info("stuck proposal returned to pending", { proposal: proposalId });
        return;
      }
      const receiptHash = marker.receipt ?? "";
      const stored = await this.server.storeNow(name, document, { proposalId, userId, receiptHash });
      if (!stored.durable) throw new ApiError(503, "store_failed");
      await this.api.finishApply(proposalId, token, { user_id: userId, resulting_counter: stored.counter, receipt_hash: receiptHash });
      this.log.info("stuck proposal finished", { proposal: proposalId, counter: stored.counter });
    });
  }
}
