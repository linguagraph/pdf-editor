import { afterEach, describe, expect, it, vi } from 'vitest';
import type { PageRpc } from '../engine/engineConnection';
import { createJobProgressStore } from '../feedback/jobRun';
import type { ToastStore } from '../feedback/toasts';
import { EN } from '../i18n/en';
import { RpcError } from '../rpc/client';
import {
  copyLarge,
  COPY_BAR_DELAY_MS,
  type EnginePlan,
  MAX_COPY_CHARS,
  runLargeCopy,
} from './largeCopy';

interface Slice {
  readonly text: string;
  readonly pages_done: number;
  readonly total: number;
  readonly done: boolean;
}

// A chunked source that yields the given slices. `yielded` counts the slices the caller took, so a
// test can see that a stop asked for no more.
function slicesRpc(slices: readonly Slice[]): {
  rpc: Pick<PageRpc, 'chunked'>;
  yielded: () => number;
} {
  let taken = 0;
  const rpc = {
    async *chunked(_method: string, _params: unknown, options?: { signal?: AbortSignal }) {
      for (const slice of slices) {
        // RpcClient.chunked asks for no slice once the signal is aborted; the fake does the same.
        if (options?.signal?.aborted === true) throw new RpcError('CANCELLED', 'cancelled');
        taken += 1;
        yield { ...slice, cursor: slice.done ? null : `c${taken}` };
      }
    },
  } as unknown as Pick<PageRpc, 'chunked'>;
  return { rpc, yielded: () => taken };
}

function slice(text: string, pagesDone: number, total: number, done: boolean): Slice {
  return { text, pages_done: pagesDone, total, done };
}

const PLAN_ENDS = { start: { page: 0, index: 0 }, end: { page: 2, index: 4 }, pages: 3 };
function plan(listen?: (fire: (reason: 'changed' | 'closed') => void) => void): EnginePlan {
  return {
    ...PLAN_ENDS,
    changes: (listener) => {
      listen?.(listener);
      return () => undefined;
    },
  };
}

function fakeStore(): ToastStore & { push: ReturnType<typeof vi.fn> } {
  return { push: vi.fn() } as unknown as ToastStore & { push: ReturnType<typeof vi.fn> };
}

// A ClipboardItem stand-in: the tests read what was written, not the browser's type.
class FakeClipboardItem {
  constructor(readonly items: Record<string, unknown>) {}
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('runLargeCopy', () => {
  it('keeps the slices that have text, in order, and asks for each slice once', async () => {
    const source = slicesRpc([
      slice('ab', 1, 3, false),
      slice('', 2, 3, false),
      slice('cd', 3, 3, true),
    ]);
    const result = await runLargeCopy(
      source.rpc,
      'h',
      plan(),
      new AbortController().signal,
      () => {},
    );
    expect(result).toEqual({ kind: 'text', parts: ['ab', 'cd'] });
    expect(source.yielded()).toBe(3);
  });

  it('reports the progress of each slice', async () => {
    const source = slicesRpc([slice('a', 1, 2, false), slice('b', 2, 2, true)]);
    const progress = vi.fn();
    await runLargeCopy(source.rpc, 'h', plan(), new AbortController().signal, progress);
    expect(progress.mock.calls).toEqual([
      [1, 2],
      [2, 2],
    ]);
  });

  it('stops after the slice that passes the limit, and asks for no more slices', async () => {
    // One character past the limit: a copy of exactly the limit is allowed.
    const big = 'x'.repeat(MAX_COPY_CHARS + 1);
    const source = slicesRpc([slice(big, 1, 5, false), slice('later', 2, 5, false)]);
    const result = await runLargeCopy(
      source.rpc,
      'h',
      plan(),
      new AbortController().signal,
      () => {},
    );
    expect(result).toEqual({ kind: 'tooLarge' });
    expect(source.yielded()).toBe(1);
  });

  it('stops when the signal is aborted, with no text', async () => {
    const source = slicesRpc([slice('a', 1, 2, false), slice('b', 2, 2, true)]);
    const controller = new AbortController();
    const result = await runLargeCopy(source.rpc, 'h', plan(), controller.signal, () =>
      controller.abort(),
    );
    expect(result).toEqual({ kind: 'cancelled' });
    expect(source.yielded()).toBe(1);
  });

  it('reports a document that changed between slices as changed', async () => {
    const rpc = {
      async *chunked() {
        yield { text: 'a', pages_done: 1, total: 2, done: false, cursor: 'c1' };
        throw new RpcError('STALE_CURSOR', 'changed');
      },
    } as unknown as Pick<PageRpc, 'chunked'>;
    const result = await runLargeCopy(rpc, 'h', plan(), new AbortController().signal, () => {});
    expect(result).toEqual({ kind: 'changed' });
  });

  it('throws any other engine error', async () => {
    // The first slice's call fails outright.
    const rpc = {
      chunked: () => ({
        [Symbol.asyncIterator]: () => ({
          next: () => Promise.reject(new RpcError('ENGINE_ERROR', 'boom')),
        }),
      }),
    } as unknown as Pick<PageRpc, 'chunked'>;
    await expect(
      runLargeCopy(rpc, 'h', plan(), new AbortController().signal, () => {}),
    ).rejects.toBeInstanceOf(RpcError);
  });
});

describe('copyLarge', () => {
  it('writes the text in the keypress, with the data as a promise, and confirms it', async () => {
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    const source = slicesRpc([slice('ab', 1, 2, false), slice('cd', 2, 2, true)]);
    const write = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copyLarge(
      { rpc: source.rpc, handle: 'h', clipboard: { write, writeText: vi.fn() } as never, store },
      plan(),
    );
    // Synchronous: the write has started before copyLarge returns.
    expect(write).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(store.push).toHaveBeenCalledWith('success', EN['copy.done']));
    const written = write.mock.calls as unknown as [FakeClipboardItem[]][];
    const blob = await (written[0]?.[0][0]?.items['text/plain'] as Promise<Blob>);
    expect(blob.size).toBe('ab\ncd'.length);
  });

  it('reports a copy the document changed during, and writes nothing', async () => {
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    const source = slicesRpc([slice('ab', 1, 2, false), slice('cd', 2, 2, true)]);
    const write = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    let fire: (reason: 'changed' | 'closed') => void = () => undefined;
    copyLarge(
      {
        rpc: source.rpc,
        handle: 'h',
        clipboard: { write, writeText: vi.fn() } as never,
        store,
      },
      plan((listener) => {
        fire = listener;
      }),
    );
    fire('changed');
    await vi.waitFor(() => expect(store.push).toHaveBeenCalledWith('info', EN['copy.changed']));
    expect(store.push).not.toHaveBeenCalledWith('success', EN['copy.done']);
  });

  it('stops quietly when the view closes, with no toast', async () => {
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    const source = slicesRpc([slice('ab', 1, 2, false), slice('cd', 2, 2, true)]);
    const write = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    let fire: (reason: 'changed' | 'closed') => void = () => undefined;
    copyLarge(
      { rpc: source.rpc, handle: 'h', clipboard: { write, writeText: vi.fn() } as never, store },
      plan((listener) => {
        fire = listener;
      }),
    );
    fire('closed');
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(store.push).not.toHaveBeenCalled();
  });

  it('shows the bar only after COPY_BAR_DELAY_MS, so a short copy does not flash it', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    const jobs = createJobProgressStore({ toasts: fakeStore() });
    const begin = vi.spyOn(jobs, 'begin');
    const source = slicesRpc([slice('ab', 1, 1, true)]);
    const store = fakeStore();
    copyLarge(
      {
        rpc: source.rpc,
        handle: 'h',
        clipboard: { write: vi.fn(() => Promise.resolve()), writeText: vi.fn() } as never,
        store,
        jobs,
      },
      plan(),
    );
    await vi.advanceTimersByTimeAsync(COPY_BAR_DELAY_MS - 1);
    expect(begin).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(COPY_BAR_DELAY_MS);
    // The copy finished during the first slice, so the bar came and went with it.
    expect(store.push).toHaveBeenCalledWith('success', EN['copy.done']);
  });

  it('is refused with copy.busy while another job holds the bar', () => {
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    const jobs = createJobProgressStore({ toasts: fakeStore() });
    jobs.begin({ token: 'other', label: 'Other' });
    const source = slicesRpc([slice('ab', 1, 1, true)]);
    const write = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copyLarge(
      {
        rpc: source.rpc,
        handle: 'h',
        clipboard: { write, writeText: vi.fn() } as never,
        store,
        jobs,
      },
      plan(),
    );
    expect(write).not.toHaveBeenCalled();
    expect(store.push).toHaveBeenCalledWith('info', EN['copy.busy']);
  });

  it('keeps the text when the browser refuses the write, and writes it on the next press', async () => {
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    const source = slicesRpc([slice('ab', 1, 1, true)]);
    const refused = Object.assign(new Error('denied'), { name: 'NotAllowedError' });
    const write = vi
      .fn()
      .mockImplementationOnce(() => Promise.reject(refused))
      .mockImplementation(() => Promise.resolve());
    const store = fakeStore();
    copyLarge(
      { rpc: source.rpc, handle: 'h', clipboard: { write, writeText: vi.fn() } as never, store },
      plan(),
    );
    await vi.waitFor(() =>
      expect(store.push).toHaveBeenCalledWith('info', EN['copy.ready'], expect.anything()),
    );
    const ready = store.push.mock.calls.find((call) => call[1] === EN['copy.ready'])?.[2] as {
      run: () => void;
    };
    ready.run();
    expect(write).toHaveBeenCalledTimes(2);
  });

  it('without ClipboardItem, waits for the text and writes it from the toast button', async () => {
    vi.stubGlobal('ClipboardItem', undefined);
    const source = slicesRpc([slice('ab', 1, 2, false), slice('cd', 2, 2, true)]);
    const writeText = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copyLarge(
      { rpc: source.rpc, handle: 'h', clipboard: { writeText, write: undefined } as never, store },
      plan(),
    );
    expect(writeText).not.toHaveBeenCalled();
    await vi.waitFor(() =>
      expect(store.push).toHaveBeenCalledWith('info', EN['copy.ready'], expect.anything()),
    );
    const ready = store.push.mock.calls.find((call) => call[1] === EN['copy.ready'])?.[2] as {
      run: () => void;
    };
    ready.run();
    expect(writeText).toHaveBeenCalledWith('ab\ncd');
  });

  it('a ClipboardItem that throws keeps the text for the next press, and a later copy still starts', async () => {
    // A browser that throws at once (not a refusal): the first copy must not leave `running` set.
    class ThrowingClipboardItem {
      constructor() {
        throw new TypeError('ClipboardItem is not supported here');
      }
    }
    vi.stubGlobal('ClipboardItem', ThrowingClipboardItem);
    const first = slicesRpc([slice('ab', 1, 1, true)]);
    const firstWrite = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copyLarge(
      {
        rpc: first.rpc,
        handle: 'h',
        clipboard: { write: firstWrite, writeText: vi.fn() } as never,
        store,
      },
      plan(),
    );
    expect(firstWrite).not.toHaveBeenCalled();
    await vi.waitFor(() =>
      expect(store.push).toHaveBeenCalledWith('info', EN['copy.ready'], expect.anything()),
    );

    // A different selection, so the kept text is not written: this is a new copy. It starts at once
    // and writes in the keypress, which only happens if the first copy released `running`.
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    const second = slicesRpc([slice('xy', 1, 1, true)]);
    const secondWrite = vi.fn(() => Promise.resolve());
    const otherPlan: EnginePlan = { ...plan(), end: { page: 1, index: 2 } };
    copyLarge(
      {
        rpc: second.rpc,
        handle: 'h',
        clipboard: { write: secondWrite, writeText: vi.fn() } as never,
        store,
      },
      otherPlan,
    );
    expect(secondWrite).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(store.push).toHaveBeenCalledWith('success', EN['copy.done']));
  });

  it('reports a change that lands after the last slice as changed, not done', async () => {
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    let fire: (reason: 'changed' | 'closed') => void = () => undefined;
    // The loop breaks after the last slice, which runs this generator's finally: the change lands
    // after the text is read and before the copy's handler runs, with the result still 'text'.
    const rpc = {
      async *chunked() {
        try {
          yield { text: 'ab', pages_done: 1, total: 1, done: true, cursor: null };
        } finally {
          fire('changed');
        }
      },
    } as unknown as Pick<PageRpc, 'chunked'>;
    const write = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copyLarge(
      { rpc, handle: 'h', clipboard: { write, writeText: vi.fn() } as never, store },
      plan((listener) => {
        fire = listener;
      }),
    );
    await vi.waitFor(() => expect(store.push).toHaveBeenCalledWith('info', EN['copy.changed']));
    expect(store.push).not.toHaveBeenCalledWith('success', EN['copy.done']);
  });
});
