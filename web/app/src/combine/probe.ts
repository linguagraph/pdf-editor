// The Combine dialog's page counts (W6.3b, PR 1). combine.probe asks the engine worker for each PDF's
// page count and encrypted flag, one file at a time, in the order the rows were added. The probe
// writes nothing and keeps nothing, and a reply only sets the state of its row. No probe is retried:
// a row that failed says so, and the job checks the file again when Combine runs.
import type { EngineClient, EngineConnection } from '../engine/engineConnection';
import { RpcError } from '../rpc/client';
import { isImageSourceName, type SourceItem } from './sources';

// The most bytes one probe sends. This is limits.MAX_PROBE_BYTES (64 MiB) in webapi/limits.py: a
// larger file is marked unchecked without a call, and a FILE_TOO_LARGE reply gives the same state.
export const PROBE_MAX_BYTES = 64 * 1024 * 1024;

export type ProbeState =
  | { readonly kind: 'pending' } // queued or in flight
  | { readonly kind: 'counted'; readonly pages: number; readonly encrypted: boolean }
  | { readonly kind: 'locked' } // password protected: the count needs a password, not yet offered
  | { readonly kind: 'unreadable' } // the engine could not open it (OPEN_FAILED)
  | { readonly kind: 'unchecked'; readonly reason: 'large' | 'engine' }; // not counted; the job will

// The state a failed probe leaves its row in. Only the codes the probe can give for a file with no
// password sent are named; anything else is an engine failure, which Combine does not block on.
export function stateOfError(error: unknown): ProbeState {
  if (error instanceof RpcError) {
    if (error.code === 'PASSWORD_REQUIRED') return { kind: 'locked' };
    if (error.code === 'OPEN_FAILED') return { kind: 'unreadable' };
    if (error.code === 'FILE_TOO_LARGE') return { kind: 'unchecked', reason: 'large' };
  }
  return { kind: 'unchecked', reason: 'engine' };
}

export type ProbeReport = (id: number, state: ProbeState) => void;

// The queue of probes for one dialog. add() queues a PDF row and reports it pending; images are
// never probed. forget() drops a row that was removed: it is not probed, and its reply is ignored.
// close() drops whatever is still waiting, a reply that comes after it is ignored, and returns the
// rows still counting so the next queue (a new engine) can count them again.
export class ProbeQueue {
  private readonly engine: Pick<EngineConnection, 'whenReady'>;
  private readonly report: ProbeReport;
  private readonly waiting: SourceItem[] = [];
  // The rows added and not yet reported or forgotten: waiting, or in flight.
  private readonly unfinished = new Map<number, SourceItem>();
  private running = false;
  private closed = false;

  constructor(engine: Pick<EngineConnection, 'whenReady'>, report: ProbeReport) {
    this.engine = engine;
    this.report = report;
  }

  add(item: SourceItem): void {
    if (this.closed || isImageSourceName(item.name)) return;
    this.report(item.id, { kind: 'pending' });
    this.unfinished.set(item.id, item);
    this.waiting.push(item);
    void this.pump();
  }

  forget(id: number): void {
    this.unfinished.delete(id);
  }

  close(): readonly SourceItem[] {
    this.closed = true;
    this.waiting.length = 0;
    const left = [...this.unfinished.values()];
    this.unfinished.clear();
    return left;
  }

  // Runs the queue in order. One probe is in flight at a time, so the viewer's engine is never
  // asked for two at once.
  private async pump(): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      for (let item = this.waiting.shift(); item !== undefined; item = this.waiting.shift()) {
        if (!this.unfinished.has(item.id)) continue; // removed before its turn
        const state = await this.probeOne(item);
        if (this.closed) return;
        if (!this.unfinished.delete(item.id)) continue; // removed while it was in flight
        this.report(item.id, state);
      }
    } finally {
      this.running = false;
    }
  }

  private async probeOne(item: SourceItem): Promise<ProbeState> {
    if (item.file.size > PROBE_MAX_BYTES) return { kind: 'unchecked', reason: 'large' };
    try {
      const engine: EngineClient = await this.engine.whenReady();
      // A fresh copy of the bytes, transferred to the engine: the page side keeps no second one.
      const data = await item.file.arrayBuffer();
      const result = await engine.call('combine.probe', { name: item.name, data, password: null });
      return { kind: 'counted', pages: result.page_count, encrypted: result.encrypted };
    } catch (error) {
      return stateOfError(error);
    }
  }
}
