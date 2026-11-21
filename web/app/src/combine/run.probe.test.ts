// Combine's checks that use the probe's page counts (W6.3b, PR 1): a locked or unreadable file blocks
// the run, and a page past the end of a counted file is refused, both before any job worker starts.
import { describe, expect, it, vi } from 'vitest';
import type { CallResult } from '../engine/loader';
import type { WorkerClient } from '../engine/start';
import { createJobProgressStore } from '../feedback/jobRun';
import { createToastStore } from '../feedback/toasts';
import { t, tp } from '../i18n/t';
import type { ProbeState } from './probe';
import { runCombine } from './run';
import type { SourceItem } from './sources';

function item(id: number, name: string, pages = ''): SourceItem {
  return { id, name, file: new File(['pdf'], name, { type: 'application/pdf' }), pages };
}

function store() {
  return createJobProgressStore({ toasts: createToastStore() });
}

// A worker that must never be asked for: the checks refuse before one is started.
function unusedWorker() {
  const call = vi.fn((): Promise<CallResult> => Promise.reject(new Error('no job expected')));
  const worker: WorkerClient = { call, onStage: () => () => undefined, terminate: vi.fn() };
  return { startWorker: vi.fn(() => worker) };
}

describe('runCombine with the probe', () => {
  it('refuses while a file is password protected, and starts no worker', async () => {
    const { startWorker } = unusedWorker();
    const probes = new Map<number, ProbeState>([[1, { kind: 'locked' }]]);
    const outcome = await runCombine([item(1, 'a.pdf'), item(2, 'b.pdf')], {
      store: store(),
      startWorker,
      probes,
    });

    expect(outcome).toEqual({ status: 'refused', message: tp('combine.blocked.locked', 1) });
    expect(startWorker).not.toHaveBeenCalled();
  });

  it('refuses while a file cannot be read, and says how many', async () => {
    const { startWorker } = unusedWorker();
    const probes = new Map<number, ProbeState>([
      [1, { kind: 'unreadable' }],
      [2, { kind: 'unreadable' }],
    ]);
    const outcome = await runCombine([item(1, 'a.pdf'), item(2, 'b.pdf')], {
      store: store(),
      startWorker,
      probes,
    });

    expect(outcome).toEqual({
      status: 'refused',
      message: tp('combine.blocked.unreadable', 2),
    });
    expect(startWorker).not.toHaveBeenCalled();
  });

  it('a file that is only unchecked does not block the run', async () => {
    const { startWorker } = unusedWorker();
    const probes = new Map<number, ProbeState>([[1, { kind: 'unchecked', reason: 'large' }]]);
    const outcome = await runCombine([item(1, 'a.pdf'), item(2, 'b.pdf')], {
      store: store(),
      startWorker,
      probes,
    });

    expect(outcome.status).not.toBe('refused');
    expect(startWorker).toHaveBeenCalledTimes(1);
  });

  it('refuses a page past the end of a counted file before any worker starts', async () => {
    const { startWorker } = unusedWorker();
    const probes = new Map<number, ProbeState>([
      [1, { kind: 'counted', pages: 2, encrypted: false }],
    ]);
    const outcome = await runCombine([item(1, 'a.pdf', '9'), item(2, 'b.pdf')], {
      store: store(),
      startWorker,
      probes,
    });

    expect(outcome).toEqual({
      status: 'rangeError',
      problem: { index: 0, kind: 'out_of_range', item: '9', count: 2 },
    });
    expect(startWorker).not.toHaveBeenCalled();
  });

  it('refuses while a row is still counting, and starts no worker', async () => {
    const { startWorker } = unusedWorker();
    const probes = new Map<number, ProbeState>([
      [1, { kind: 'counted', pages: 2, encrypted: false }],
      [2, { kind: 'pending' }],
    ]);
    const outcome = await runCombine([item(1, 'a.pdf'), item(2, 'b.pdf')], {
      store: store(),
      startWorker,
      probes,
    });

    expect(outcome).toEqual({ status: 'refused', message: t('combine.blocked.pending') });
    expect(startWorker).not.toHaveBeenCalled();
  });
});
