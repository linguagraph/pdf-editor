import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EngineClient, EngineConnection } from '../engine/engineConnection';
import type { CallResult, Notice } from '../engine/loader';
import type { WorkerClient } from '../engine/start';
import { createJobProgressStore } from '../feedback/jobRun';
import { createToastStore } from '../feedback/toasts';
import { t, tp } from '../i18n/t';
import { RpcError } from '../rpc/client';
import { CombineDialog } from './CombineDialog';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom has no CSS.escape, and react-aria calls it when a modal opens.
if (typeof (globalThis as { CSS?: { escape?: unknown } }).CSS?.escape !== 'function') {
  Object.defineProperty(globalThis, 'CSS', {
    configurable: true,
    value: { escape: (value: string) => value.replace(/[^\w-]/g, (c) => `\\${c}`) },
  });
}

function mergeReply(output: ArrayBuffer): CallResult {
  return {
    reply: JSON.stringify({
      v: 1,
      id: 1,
      ok: true,
      result: {
        kind: 'new_document',
        label: '',
        require_full_save: false,
        document: { $bin: 0 },
        files: [],
        value: null,
        name: 'merged.pdf',
      },
      events: [],
    }),
    buffer: output,
  };
}

// A job worker that answers every job.run with a merged document.
function mergingWorker(output: ArrayBuffer): WorkerClient {
  return {
    call: (): Promise<CallResult> => Promise.resolve(mergeReply(output)),
    onStage: () => () => undefined,
    terminate: vi.fn(),
  };
}

// A job worker whose job.run waits for the test: the test sends the job's notices, then finishes it.
function pendingWorker(output: ArrayBuffer) {
  let onNotice: ((notice: Notice) => void) | undefined;
  let resolveCall: ((result: CallResult) => void) | undefined;
  const worker: WorkerClient = {
    call: (_request, _buffers, notice): Promise<CallResult> => {
      onNotice = notice;
      return new Promise<CallResult>((resolve) => {
        resolveCall = resolve;
      });
    },
    onStage: () => () => undefined,
    terminate: vi.fn(),
  };
  return {
    worker,
    notify: (notice: Notice): void => onNotice?.(notice),
    finish: (): void => resolveCall?.(mergeReply(output)),
  };
}

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  host.remove();
  document.body.innerHTML = '';
});

function fileInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (input === null) throw new Error('no file input');
  return input;
}

function buttonNamed(text: string): HTMLButtonElement {
  const found = [...document.querySelectorAll<HTMLButtonElement>('button')].find(
    (button) => button.textContent?.trim() === text,
  );
  if (found === undefined) throw new Error(`no button ${text}`);
  return found;
}

// Picks files through the hidden file input, as the browser does after the user chooses them.
function pick(...names: string[]): void {
  const input = fileInput();
  const files = names.map((name) => new File(['pdf'], name, { type: 'application/pdf' }));
  Object.defineProperty(input, 'files', { configurable: true, value: files });
  act(() => {
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function rowNames(): string[] {
  return [...document.querySelectorAll('.combine-row .combine-name')].map(
    (node) => node.textContent ?? '',
  );
}

// The row list item of the named file.
function rowNamed(name: string): Element | undefined {
  return [...document.querySelectorAll('.combine-row')].find(
    (row) => row.querySelector('.combine-name')?.textContent === name,
  );
}

function render(props: {
  startWorker?: () => WorkerClient;
  download?: (name: string, bytes: ArrayBuffer) => void;
  engine?: Pick<EngineConnection, 'whenReady'>;
}) {
  const store = createJobProgressStore({ toasts: createToastStore() });
  act(() => {
    root.render(
      <CombineDialog
        isOpen
        onClose={() => undefined}
        store={store}
        startWorker={props.startWorker ?? (() => mergingWorker(new ArrayBuffer(0)))}
        download={props.download ?? (() => undefined)}
        engine={props.engine}
      />,
    );
  });
  return store;
}

// An engine whose combine.probe answers each file by its name: a page count, or an RpcError.
function probingEngine(answer: (name: string) => { pages: number } | RpcError) {
  const client = {
    call: (_method: string, params: { name: string }): Promise<unknown> => {
      const reply = answer(params.name);
      if (reply instanceof RpcError) return Promise.reject(reply);
      return Promise.resolve({ page_count: reply.pages, encrypted: false, needed_password: false });
    },
  };
  return { whenReady: () => Promise.resolve(client as unknown as EngineClient) };
}

// Lets the probe queue run to the end: each probe is a few awaits, so a few turns are enough.
async function settle(): Promise<void> {
  for (let turn = 0; turn < 5; turn++) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

describe('CombineDialog', () => {
  it('lists the added sources in order and moves, removes and renames nothing else', () => {
    render({});

    pick('a.pdf', 'b.png', 'c.pdf');
    expect(rowNames()).toEqual(['a.pdf', 'b.png', 'c.pdf']);

    act(() => {
      buttonNamed(t('combine.moveUp')).click(); // the first row is at the top: no change
    });
    const moveDown = [...document.querySelectorAll<HTMLButtonElement>('.combine-row')][1];
    act(() => {
      moveDown?.querySelectorAll('button')[1]?.click(); // b.png moves down past c.pdf
    });
    expect(rowNames()).toEqual(['a.pdf', 'c.pdf', 'b.png']);

    const firstRow = document.querySelector('.combine-row');
    act(() => {
      firstRow?.querySelectorAll('button')[2]?.click(); // remove a.pdf
    });
    expect(rowNames()).toEqual(['c.pdf', 'b.png']);
  });

  it('says which dropped or picked files were not PDFs or images and adds the rest', () => {
    render({});

    pick('notes.txt', 'a.pdf');

    expect(rowNames()).toEqual(['a.pdf']);
    expect(document.querySelector('[role="alert"]')?.textContent).toBe(
      t('combine.unsupported', { name: 'notes.txt' }),
    );
  });

  it('refuses fewer than two sources with a message and starts no job', async () => {
    const startWorker = vi.fn(() => mergingWorker(new ArrayBuffer(0)));
    render({ startWorker });
    pick('a.pdf');

    await act(async () => {
      buttonNamed(t('combine.button')).click();
    });

    expect(document.querySelector('[role="alert"]')?.textContent).toBe(t('combine.needTwo'));
    expect(startWorker).not.toHaveBeenCalled();
  });

  it('combines the sources in order and offers the result as a download', async () => {
    const output = new Uint8Array([1, 2, 3]).buffer;
    const download = vi.fn();
    const store = render({ startWorker: () => mergingWorker(output), download });
    pick('a.pdf', 'b.pdf');

    await act(async () => {
      buttonNamed(t('combine.button')).click();
    });

    expect(store.getState().job).toBeUndefined();
    expect(document.querySelector('[role="status"]')?.textContent).toContain(t('combine.done'));
    await act(async () => {
      buttonNamed(t('combine.download', { name: 'merged.pdf' })).click();
    });
    expect(download).toHaveBeenCalledWith('merged.pdf', output);
  });

  it('shows the running line while the job runs, and the bar moves with its notices', async () => {
    const output = new Uint8Array([7]).buffer;
    const job = pendingWorker(output);
    const store = render({ startWorker: () => job.worker });
    pick('a.pdf', 'b.pdf');

    await act(async () => {
      buttonNamed(t('combine.button')).click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(document.querySelector('[role="status"]')?.textContent).toBe(t('combine.running'));

    act(() => {
      job.notify({ notice: 'progress', id: 1, data: { done: 1, total: 2, status: '' } });
    });
    expect(store.getState().job?.percent).toBe(50);

    await act(async () => {
      job.finish();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(document.querySelector('.combine-status')).toBeNull();
    expect(document.querySelector('[role="status"]')?.textContent).toContain(t('combine.done'));
  });

  it('uses the output name the user typed for the download', async () => {
    const output = new Uint8Array([4]).buffer;
    const download = vi.fn();
    render({ startWorker: () => mergingWorker(output), download });
    pick('a.pdf', 'b.pdf');
    const name = document.querySelector<HTMLInputElement>('.combine-output input');
    act(() => {
      if (name !== null) {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        setter?.call(name, 'report');
        name.dispatchEvent(new Event('input', { bubbles: true }));
        name.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });

    await act(async () => {
      buttonNamed(t('combine.button')).click();
    });
    await act(async () => {
      buttonNamed(t('combine.download', { name: 'report.pdf' })).click();
    });

    expect(download).toHaveBeenCalledWith('report.pdf', output);
  });
});

// Types into a Pages box the way the user does: the value, then an input event.
function typeInto(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  act(() => {
    setter?.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function pagesBoxes(): HTMLInputElement[] {
  return [...document.querySelectorAll<HTMLInputElement>('.combine-pages input')];
}

// A job worker that refuses the job with a page_range error on the given source index.
function rangeRefusingWorker(index: number, pageCount: number): WorkerClient {
  return {
    call: (): Promise<CallResult> =>
      Promise.resolve({
        reply: JSON.stringify({
          v: 1,
          id: 1,
          ok: false,
          error: {
            code: 'INVALID_ARGS',
            message: 'a range does not fit',
            details: {
              path: `args.sources[${index}].ranges`,
              reason: 'page_range',
              kind: 'out_of_range',
              item: '9',
              page_count: pageCount,
            },
          },
          events: [],
        }),
      }),
    onStage: () => () => undefined,
    terminate: vi.fn(),
  };
}

describe('CombineDialog page ranges', () => {
  it('gives a Pages box to PDF rows only and says Image for the rest', () => {
    render({});
    pick('a.pdf', 'b.png');

    expect(pagesBoxes()).toHaveLength(1);
    expect(pagesBoxes()[0]?.getAttribute('aria-label')).toBe(
      t('combine.pages.labelFor', { name: 'a.pdf' }),
    );
    expect(document.querySelector('.combine-pages-image')?.textContent).toBe(t('combine.image'));
  });

  it('marks a box that does not fit when it loses focus, and clears the mark when edited', () => {
    render({});
    pick('a.pdf');
    const box = pagesBoxes()[0];
    if (box === undefined) throw new Error('no Pages box');

    typeInto(box, '3-2');
    act(() => {
      box.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
    });

    expect(box.getAttribute('aria-invalid')).toBe('true');
    const error = document.getElementById(box.getAttribute('aria-describedby') ?? '');
    expect(error?.textContent).toBe(t('combine.error.ranges.invalid', { item: '3-2' }));

    typeInto(box, '3-20');
    expect(box.getAttribute('aria-invalid')).toBeNull();
  });

  it('refuses a bad box before any job starts and focuses it', async () => {
    const startWorker = vi.fn(() => mergingWorker(new ArrayBuffer(0)));
    render({ startWorker });
    pick('a.pdf', 'b.pdf');
    const box = pagesBoxes()[1];
    if (box === undefined) throw new Error('no Pages box');
    typeInto(box, 'x');

    await act(async () => {
      buttonNamed(t('combine.button')).click();
    });

    expect(startWorker).not.toHaveBeenCalled();
    expect(document.querySelector('[role="alert"]')?.textContent).toBe(
      t('combine.error.ranges.summary'),
    );
    expect(document.activeElement).toBe(box);
  });

  it("shows the job's out_of_range on its own row, also after a reorder", async () => {
    render({ startWorker: () => rangeRefusingWorker(1, 3) });
    pick('a.pdf', 'b.pdf');
    const box = pagesBoxes()[1];
    if (box === undefined) throw new Error('no Pages box');
    typeInto(box, '9');

    await act(async () => {
      buttonNamed(t('combine.button')).click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const message = tp('combine.error.ranges.outOfRange', 3, { item: '9', name: 'b.pdf' });
    const rowOf = (name: string): Element | undefined =>
      [...document.querySelectorAll('.combine-row')].find(
        (row) => row.querySelector('.combine-name')?.textContent === name,
      );
    expect(rowOf('b.pdf')?.textContent).toContain(message);
    expect(rowOf('a.pdf')?.textContent).not.toContain(message);

    act(() => {
      const moveUp = rowOf('b.pdf')?.querySelectorAll('button')[0];
      moveUp?.click();
    });
    expect(rowOf('b.pdf')?.textContent).toContain(message);
  });
});

describe('CombineDialog page counts', () => {
  it('shows each file its pages and where they land, and the total of the result', async () => {
    render({ engine: probingEngine((name) => ({ pages: name === 'a.pdf' ? 3 : 2 })) });
    pick('a.pdf', 'b.pdf');
    await settle();

    expect(rowNamed('a.pdf')?.textContent).toContain(tp('combine.row.pages', 3));
    expect(rowNamed('a.pdf')?.textContent).toContain(
      t('combine.row.placed', { first: 1, last: 3 }),
    );
    expect(rowNamed('b.pdf')?.textContent).toContain(
      t('combine.row.placed', { first: 4, last: 5 }),
    );
    expect(document.querySelector('.combine-preview')?.textContent).toBe(
      tp('combine.preview.total', 5),
    );
  });

  it('refuses Combine with the locked message when a file is password protected', async () => {
    const startWorker = vi.fn(() => mergingWorker(new ArrayBuffer(0)));
    render({
      startWorker,
      engine: probingEngine((name) =>
        name === 'a.pdf' ? new RpcError('PASSWORD_REQUIRED', 'protected') : { pages: 2 },
      ),
    });
    pick('a.pdf', 'b.pdf');
    await settle();

    expect(rowNamed('a.pdf')?.textContent).toContain(t('combine.row.locked'));
    act(() => {
      buttonNamed(t('combine.button')).click();
    });
    await settle();

    expect(startWorker).not.toHaveBeenCalled();
    expect(document.querySelector('[role="alert"]')?.textContent).toBe(
      tp('combine.blocked.locked', 1),
    );
  });

  it('marks a Pages box past the end of a counted file on its row, before Combine', async () => {
    const startWorker = vi.fn(() => mergingWorker(new ArrayBuffer(0)));
    render({ startWorker, engine: probingEngine(() => ({ pages: 2 })) });
    pick('a.pdf', 'b.pdf');
    await settle();

    const box = pagesBoxes()[0];
    if (box === undefined) throw new Error('no Pages box');
    typeInto(box, '9');
    act(() => {
      box.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
    });

    const error = document.getElementById(box.getAttribute('aria-describedby') ?? '');
    expect(error?.textContent).toBe(
      tp('combine.error.ranges.outOfRange', 2, { item: '9', name: 'a.pdf' }),
    );
    act(() => {
      buttonNamed(t('combine.button')).click();
    });
    await settle();
    expect(startWorker).not.toHaveBeenCalled();
  });

  it('does not probe a row that was removed before its probe ran', async () => {
    const engine = manualProbeEngine();
    render({ engine: engine.engine });
    pick('a.pdf', 'b.pdf');
    await settle();

    act(() => {
      rowNamed('b.pdf')?.querySelectorAll('button')[2]?.click(); // remove b.pdf while a.pdf counts
    });
    engine.answer('a.pdf', { pages: 3 });
    await settle();

    expect(engine.names()).toEqual(['a.pdf']);
  });

  it('counts the rows still pending again when the engine changes', async () => {
    const startWorker = vi.fn(() => mergingWorker(new ArrayBuffer(0)));
    const stalled = manualProbeEngine();
    render({ startWorker, engine: stalled.engine });
    pick('a.pdf', 'b.pdf');
    await settle();

    render({ startWorker, engine: probingEngine(() => ({ pages: 2 })) });
    await settle();
    await act(async () => {
      buttonNamed(t('combine.button')).click();
    });

    expect(stalled.names()).toEqual(['a.pdf']);
    expect(startWorker).toHaveBeenCalledTimes(1);
  });

  it('does not refuse Combine for a row still counting when the engine goes away', async () => {
    const startWorker = vi.fn(() => mergingWorker(new ArrayBuffer(0)));
    const stalled = manualProbeEngine();
    render({ startWorker, engine: stalled.engine });
    pick('a.pdf', 'b.pdf');
    await settle();
    expect(rowNamed('a.pdf')?.textContent).toContain(t('combine.row.counting'));

    render({ startWorker, engine: undefined });
    await settle();
    expect(rowNamed('a.pdf')?.textContent).not.toContain(t('combine.row.counting'));

    await act(async () => {
      buttonNamed(t('combine.button')).click();
    });
    await settle();

    expect(document.querySelector('[role="alert"]')?.textContent).not.toBe(
      t('combine.blocked.pending'),
    );
    expect(startWorker).toHaveBeenCalledTimes(1);
  });
});

// An engine that holds each probe until the test answers it, and names the probes it was asked.
function manualProbeEngine() {
  const calls: { name: string; resolve: (value: unknown) => void }[] = [];
  const client = {
    call: (_method: string, params: { name: string }): Promise<unknown> =>
      new Promise<unknown>((resolve) => {
        calls.push({ name: params.name, resolve });
      }),
  };
  return {
    engine: { whenReady: () => Promise.resolve(client as unknown as EngineClient) },
    names: (): string[] => calls.map((call) => call.name),
    answer: (name: string, reply: { pages: number }): void => {
      const call = calls.find((candidate) => candidate.name === name);
      call?.resolve({ page_count: reply.pages, encrypted: false, needed_password: false });
    },
  };
}
