/**
 * The single-instance guarantee. Only the holder of the lease may write. The lease row in PostgreSQL
 * carries a fencing token that increases on every acquisition; every write sends it and the API
 * refuses a stale one inside the write transaction (409 stale_lease). So even a process that was
 * paused for a minute and wakes up believing it still holds the lease cannot write.
 *
 *  - acquire(): retries until it succeeds (a crashed predecessor's lease expires within the TTL);
 *  - renew every leaseRenewMs; a 409 means someone else took over: onLost() immediately;
 *  - if the API cannot be reached for longer than leaseGraceMs (shorter than the TTL) we stop trusting
 *    our lease and call onLost() too, because by then another instance could legitimately hold it.
 */
import { randomUUID } from "node:crypto";
import { ApiError, type InternalApi } from "./api.js";
import type { Logger } from "./log.js";

export class LeaseKeeper {
  readonly holderId = `collab-${randomUUID()}`;
  private currentToken: number | null = null;
  private timer: NodeJS.Timeout | null = null;
  private lastOkAt = 0;
  private lost = false;
  private stopped = false;

  constructor(
    private readonly api: InternalApi,
    private readonly log: Logger,
    private readonly opts: { renewMs: number; graceMs: number; onLost: () => void },
  ) {}

  /** The token to send with writes. Throws if the lease is not held (a write must not even be tried). */
  get token(): number {
    if (this.currentToken === null || this.lost) throw new ApiError(409, "stale_lease");
    return this.currentToken;
  }
  get held(): boolean {
    return this.currentToken !== null && !this.lost && !this.stopped;
  }

  async acquire(maxWaitMs = 120_000, retryMs = 2_000): Promise<void> {
    const deadline = Date.now() + maxWaitMs;
    for (;;) {
      try {
        const lease = await this.api.acquireLease(this.holderId);
        this.currentToken = lease.token;
        this.lastOkAt = Date.now();
        this.log.info("lease acquired", { fencing: lease.token });
        this.timer = setInterval(() => void this.renew(), this.opts.renewMs);
        this.timer.unref?.();
        return;
      } catch (error) {
        if (Date.now() >= deadline) throw error;
        const held = error instanceof ApiError && error.code === "lease_held";
        this.log.warn(held ? "lease held by another instance, waiting" : "lease acquire failed, retrying", {
          code: error instanceof ApiError ? error.code : "error",
        });
        await new Promise((resolve) => setTimeout(resolve, retryMs));
      }
    }
  }

  private async renew(): Promise<void> {
    if (this.stopped || this.lost || this.currentToken === null) return;
    try {
      await this.api.renewLease(this.holderId, this.currentToken);
      this.lastOkAt = Date.now();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) return this.lose("stale_lease");
      this.log.warn("lease renew failed", { code: error instanceof ApiError ? error.code : "error" });
      if (Date.now() - this.lastOkAt > this.opts.graceMs) this.lose("renew_unreachable");
    }
  }

  /** Called by a write that was refused with stale_lease: the lease is gone, whatever the timer thinks. */
  noteStale(): void {
    this.lose("stale_lease_on_write");
  }

  private lose(reason: string): void {
    if (this.lost) return;
    this.lost = true;
    if (this.timer) clearInterval(this.timer);
    this.log.error("lease lost", { reason });
    this.opts.onLost();
  }

  /** Release on graceful shutdown so the next instance does not wait for the TTL. */
  async release(): Promise<void> {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    if (this.currentToken === null || this.lost) return;
    try {
      await this.api.releaseLease(this.holderId, this.currentToken);
      this.log.info("lease released");
    } catch (error) {
      this.log.warn("lease release failed", { code: error instanceof ApiError ? error.code : "error" });
    }
  }
}
