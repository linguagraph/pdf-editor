// The combine flow (W6.3): checks the sources, starts the merge job on a job worker, reports it to
// the job store (progress bar, Cancel, error toast) and returns the merged bytes for the download.
// Nothing here draws; the dialog calls it and shows the outcome.
import { MAX_FILE_BYTES } from '../documents/openFile';
import type { WorkerClient } from '../engine/start';
import { RpcError } from '../rpc/client';
import { t, tp } from '../i18n/t';
import type { JobProgressStore } from '../feedback/jobRun';
import { parsePageRanges, type PageRangeKind } from '../pages/pageRanges';
import type { ProbeState } from './probe';
import { runMergeJob } from './merge';
import { isImageSourceName, MIN_SOURCES, type SourceItem } from './sources';

// A row whose Pages text does not fit. index is the row's place in the list; count is the file's
// page count when the job knew it (out_of_range only), else null.
export interface RangeProblem {
  readonly index: number;
  readonly kind: PageRangeKind;
  readonly item: string;
  readonly count: number | null;
}

export type CombineOutcome =
  | { readonly status: 'done'; readonly name: string; readonly bytes: ArrayBuffer }
  | { readonly status: 'refused'; readonly message: string }
  | { readonly status: 'rangeError'; readonly problem: RangeProblem }
  | { readonly status: 'cancelled' }
  | { readonly status: 'failed'; readonly error: RpcError };

export interface CombineDeps {
  readonly store: JobProgressStore;
  // A new job worker for this job. The flow terminates it when the job ends.
  readonly startWorker: () => WorkerClient;
  // The rows' page counts, from the probe. A counted row's Pages text is checked in full mode.
  readonly probes?: ReadonlyMap<number, ProbeState>;
}

const NO_PROBES: ReadonlyMap<number, ProbeState> = new Map();

let nextToken = 1;

// Converts whatever a failed read or call threw into an RpcError, as the other flows do.
function asRpcError(error: unknown): RpcError {
  if (error instanceof RpcError) return error;
  const message = error instanceof Error ? error.message : String(error);
  return new RpcError('INTERNAL', `the files could not be combined: ${message}`);
}

// The checks that come before any job starts: at least two sources, no file above the same 1 GiB
// limit that opening uses, and no row the probe could not read (a password-protected or unreadable
// file cannot be combined yet). A refusal returns the message to show, and starts nothing.
export function checkSources(
  items: readonly SourceItem[],
  probes: ReadonlyMap<number, ProbeState> = NO_PROBES,
): string | null {
  if (items.length < MIN_SOURCES) return t('combine.needTwo');
  const big = items.find((item) => item.file.size > MAX_FILE_BYTES);
  if (big !== undefined) return t('combine.tooLarge', { name: big.name });
  const locked = countOf(items, probes, 'locked');
  if (locked > 0) return tp('combine.blocked.locked', locked);
  const unreadable = countOf(items, probes, 'unreadable');
  if (unreadable > 0) return tp('combine.blocked.unreadable', unreadable);
  // A row still counting may turn out locked or unreadable: the job would fail with a generic
  // message, so Combine waits for the count instead.
  if (countOf(items, probes, 'pending') > 0) return t('combine.blocked.pending');
  return null;
}

// How many rows the probe put in the given state.
function countOf(
  items: readonly SourceItem[],
  probes: ReadonlyMap<number, ProbeState>,
  kind: ProbeState['kind'],
): number {
  return items.filter((item) => probes.get(item.id)?.kind === kind).length;
}

// Every PDF row whose Pages text is not blank and does not fit the page-range rule. A row whose page
// count is known is checked in full mode, so a page past the end is found here; the others in syntax
// mode, and the job stays the authority for them. Empty when all fit.
export function rangeProblems(
  items: readonly SourceItem[],
  probes: ReadonlyMap<number, ProbeState> = NO_PROBES,
): readonly RangeProblem[] {
  const problems: RangeProblem[] = [];
  items.forEach((item, index) => {
    if (isImageSourceName(item.name) || item.pages.trim() === '') return;
    const state = probes.get(item.id);
    const count = state?.kind === 'counted' ? state.pages : null;
    const result = parsePageRanges(item.pages, count);
    if (!result.ok) {
      const outOfRange = result.error.kind === 'out_of_range';
      problems.push({
        index,
        kind: result.error.kind,
        item: result.error.item,
        count: outOfRange ? count : null,
      });
    }
  });
  return problems;
}

const RANGE_KINDS: readonly string[] = ['invalid', 'out_of_range', 'empty'];
const SOURCE_RANGE_PATH = /^args\.sources\[(\d+)\]\.ranges$/;

// The job's page_range refusal as a row problem, or null when the error is anything else.
function rangeProblemOf(error: RpcError): RangeProblem | null {
  const details = error.details;
  if (error.code !== 'INVALID_ARGS' || details.reason !== 'page_range') return null;
  const match = typeof details.path === 'string' ? SOURCE_RANGE_PATH.exec(details.path) : null;
  if (match === null || match[1] === undefined) return null;
  if (typeof details.kind !== 'string' || !RANGE_KINDS.includes(details.kind)) return null;
  return {
    index: Number(match[1]),
    kind: details.kind as PageRangeKind,
    item: typeof details.item === 'string' ? details.item : '',
    count: typeof details.page_count === 'number' ? details.page_count : null,
  };
}

// Runs the combine. The job's token is local (merge has no prepare); the store shows the bar, and
// finish reports the outcome, with an error toast when the job fails. A bad Pages box is reported
// as rangeError with no toast: the dialog shows it on the row.
export async function runCombine(
  items: readonly SourceItem[],
  deps: CombineDeps,
): Promise<CombineOutcome> {
  const probes = deps.probes ?? NO_PROBES;
  const refusal = checkSources(items, probes);
  if (refusal !== null) return { status: 'refused', message: refusal };
  const [first] = rangeProblems(items, probes);
  if (first !== undefined) return { status: 'rangeError', problem: first };

  const worker = deps.startWorker();
  const token = `merge-${nextToken++}`;
  let cancelled = false;
  const stop = (): void => {
    cancelled = true;
    worker.terminate();
  };
  if (!deps.store.begin({ token, label: t('combine.label'), terminate: stop })) {
    worker.terminate();
    return { status: 'refused', message: t('combine.busy') };
  }

  try {
    const sources = await Promise.all(
      items.map(async (item) => {
        const data = await item.file.arrayBuffer();
        const typed = isImageSourceName(item.name) || item.pages.trim() === '' ? null : item.pages;
        return typed === null
          ? { name: item.name, data }
          : { name: item.name, data, ranges: typed };
      }),
    );
    const output = await runMergeJob(worker, sources, (percent) => {
      deps.store.progress(token, percent);
    });
    deps.store.finish(token, { ok: true });
    return { status: 'done', name: output.name, bytes: output.bytes };
  } catch (error) {
    if (cancelled) return { status: 'cancelled' };
    const rpcError = asRpcError(error);
    const problem = rangeProblemOf(rpcError);
    if (problem !== null) {
      deps.store.finish(token, { ok: false, error: rpcError, quiet: true });
      return { status: 'rangeError', problem };
    }
    deps.store.finish(token, { ok: false, error: rpcError });
    return { status: 'failed', error: rpcError };
  } finally {
    worker.terminate();
  }
}
