import { describe, expect, it, vi } from 'vitest';
import type { EngineClient, EngineConnection } from '../engine/engineConnection';
import { RpcError } from '../rpc/client';
import { PROBE_MAX_BYTES, ProbeQueue, type ProbeState, stateOfError } from './probe';
import type { SourceItem } from './sources';

function pdf(id: number, name: string, size = 3): SourceItem {
  const file = new File(['x'.repeat(size)], name, { type: 'application/pdf' });
  return { id, name, file, pages: '' };
}

// A call the test answers by hand, so the order and the overlap of the probes can be seen.
function manualEngine() {
  const calls: { name: string; resolve: (value: unknown) => void; reject: (e: Error) => void }[] =
    [];
  const call = vi.fn(
    (_method: string, params: { name: string }) =>
      new Promise<unknown>((resolve, reject) => {
        calls.push({ name: params.name, resolve, reject });
      }),
  );
  const client = { call } as unknown as EngineClient;
  const engine: Pick<EngineConnection, 'whenReady'> = {
    whenReady: () => Promise.resolve(client),
  };
  return { engine, call, calls };
}

// Lets the queue's awaits run to the end.
async function flush(): Promise<void> {
  for (let turn = 0; turn < 5; turn++) await new Promise((resolve) => setTimeout(resolve, 0));
}

const counted = (pages: number): unknown => ({
  page_count: pages,
  encrypted: false,
  needed_password: false,
});

describe('stateOfError', () => {
  it.each([
    ['PASSWORD_REQUIRED', { kind: 'locked' }],
    ['OPEN_FAILED', { kind: 'unreadable' }],
    ['FILE_TOO_LARGE', { kind: 'unchecked', reason: 'large' }],
    ['INTERNAL', { kind: 'unchecked', reason: 'engine' }],
    ['WRONG_PASSWORD', { kind: 'unchecked', reason: 'engine' }],
  ] as const)('%s gives its row state', (code, state) => {
    expect(stateOfError(new RpcError(code, 'x'))).toEqual(state);
  });

  it('any other failure is an engine failure, not a block', () => {
    expect(stateOfError(new Error('network'))).toEqual({ kind: 'unchecked', reason: 'engine' });
  });
});

describe('ProbeQueue', () => {
  it('runs one probe at a time, in the order the rows were added', async () => {
    const { engine, call, calls } = manualEngine();
    const reports: [number, ProbeState][] = [];
    const queue = new ProbeQueue(engine, (id, state) => reports.push([id, state]));

    queue.add(pdf(1, 'a.pdf'));
    queue.add(pdf(2, 'b.pdf'));
    await flush();
    expect(call).toHaveBeenCalledTimes(1);
    expect(calls[0]?.name).toBe('a.pdf');

    calls[0]?.resolve(counted(3));
    await flush();
    expect(call).toHaveBeenCalledTimes(2);
    expect(calls[1]?.name).toBe('b.pdf');
    calls[1]?.resolve(counted(2));
    await flush();

    expect(reports).toContainEqual([1, { kind: 'counted', pages: 3, encrypted: false }]);
    expect(reports).toContainEqual([2, { kind: 'counted', pages: 2, encrypted: false }]);
  });

  it('reports a row pending when it is added, and never probes an image', async () => {
    const { engine, call } = manualEngine();
    const reports: [number, ProbeState][] = [];
    const queue = new ProbeQueue(engine, (id, state) => reports.push([id, state]));

    queue.add(pdf(1, 'scan.png'));
    queue.add(pdf(2, 'a.pdf'));
    await flush();

    expect(reports).toEqual([[2, { kind: 'pending' }]]);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('sends the name and the bytes, with no password', async () => {
    const { engine, call, calls } = manualEngine();
    const queue = new ProbeQueue(engine, () => undefined);
    queue.add(pdf(1, 'a.pdf', 5));
    await flush();

    expect(call).toHaveBeenCalledWith('combine.probe', {
      name: 'a.pdf',
      data: expect.any(ArrayBuffer) as ArrayBuffer,
      password: null,
    });
    calls[0]?.resolve(counted(1));
  });

  it('a file above the limit is unchecked and makes no call', async () => {
    const { engine, call } = manualEngine();
    const reports: [number, ProbeState][] = [];
    const queue = new ProbeQueue(engine, (id, state) => reports.push([id, state]));
    const big = pdf(1, 'big.pdf');
    Object.defineProperty(big.file, 'size', { value: PROBE_MAX_BYTES + 1 });

    queue.add(big);
    await flush();

    expect(call).not.toHaveBeenCalled();
    expect(reports).toContainEqual([1, { kind: 'unchecked', reason: 'large' }]);
  });

  it('a failed probe is reported once and never retried', async () => {
    const { engine, call, calls } = manualEngine();
    const reports: [number, ProbeState][] = [];
    const queue = new ProbeQueue(engine, (id, state) => reports.push([id, state]));
    queue.add(pdf(1, 'a.pdf'));
    await flush();

    calls[0]?.reject(new RpcError('INTERNAL', 'boom'));
    await flush();

    expect(call).toHaveBeenCalledTimes(1);
    expect(reports.at(-1)).toEqual([1, { kind: 'unchecked', reason: 'engine' }]);
  });

  it('drops a reply that comes after close, and what was still waiting', async () => {
    const { engine, call, calls } = manualEngine();
    const reports: [number, ProbeState][] = [];
    const queue = new ProbeQueue(engine, (id, state) => reports.push([id, state]));
    queue.add(pdf(1, 'a.pdf'));
    queue.add(pdf(2, 'b.pdf'));
    await flush();

    queue.close();
    calls[0]?.resolve(counted(3));
    await flush();

    expect(call).toHaveBeenCalledTimes(1);
    expect(reports.some(([id, state]) => id === 1 && state.kind === 'counted')).toBe(false);
  });

  it('a row removed before its probe runs is not probed', async () => {
    const { engine, call, calls } = manualEngine();
    const reports: [number, ProbeState][] = [];
    const queue = new ProbeQueue(engine, (id, state) => reports.push([id, state]));
    queue.add(pdf(1, 'a.pdf'));
    queue.add(pdf(2, 'b.pdf'));
    await flush();

    queue.forget(2); // b.pdf is removed while a.pdf is in flight
    calls[0]?.resolve(counted(3));
    await flush();

    expect(call).toHaveBeenCalledTimes(1);
    expect(calls[0]?.name).toBe('a.pdf');
    expect(reports.some(([id, state]) => id === 2 && state.kind === 'counted')).toBe(false);
  });

  it('a row removed while its probe is in flight gets no reply', async () => {
    const { engine, calls } = manualEngine();
    const reports: [number, ProbeState][] = [];
    const queue = new ProbeQueue(engine, (id, state) => reports.push([id, state]));
    queue.add(pdf(1, 'a.pdf'));
    await flush();

    queue.forget(1);
    calls[0]?.resolve(counted(3));
    await flush();

    expect(reports).toEqual([[1, { kind: 'pending' }]]);
  });

  it('close hands back the rows still counting, so a new queue can count them', async () => {
    const { engine, call, calls } = manualEngine();
    const first = new ProbeQueue(engine, () => undefined);
    first.add(pdf(1, 'a.pdf'));
    first.add(pdf(2, 'b.pdf'));
    await flush();

    const left = first.close();
    expect(left.map((item) => item.id)).toEqual([1, 2]);

    const reports: [number, ProbeState][] = [];
    const second = new ProbeQueue(engine, (id, state) => reports.push([id, state]));
    for (const item of left) second.add(item);
    await flush();
    expect(call).toHaveBeenCalledTimes(2);
    expect(calls[1]?.name).toBe('a.pdf');
  });
});

describe('the probe size limit', () => {
  it('is the same 64 MiB the engine checks (webapi/limits.py MAX_PROBE_BYTES)', () => {
    expect(PROBE_MAX_BYTES).toBe(64 * 1024 * 1024);
  });
});
