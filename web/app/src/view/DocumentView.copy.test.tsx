import { act, createRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PageRpc } from '../engine/engineConnection';
import type { ToastStore } from '../feedback/toasts';
import { EN } from '../i18n/en';
import { copySelectedText } from '../text/copy';
import { toasts } from '../feedback/toasts';
import { PREFS_STORAGE_KEY } from '../prefs/store';
import { DocumentView, type DocumentViewHandle } from './DocumentView';
import { RenderScheduler } from './renderScheduler';
import type { ViewTool } from './viewTool';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Page 0 reads "ab" and "cd" on two lines (the layer of copy.test.ts); each page has the same text.
function joinChars(boxes: Float32Array, numbers: Uint32Array): ArrayBuffer {
  const joined = new Uint8Array(boxes.byteLength + numbers.byteLength);
  joined.set(new Uint8Array(boxes.buffer), 0);
  joined.set(new Uint8Array(numbers.buffer), boxes.byteLength);
  return joined.buffer;
}

const TEXT_LAYER = {
  revision: 1,
  text: 'ab\ncd',
  line_rects: [
    { x0: 10, y0: 20, x1: 30, y1: 30 },
    { x0: 10, y0: 40, x1: 31, y1: 52 },
  ],
  chars: joinChars(
    new Float32Array([10, 20, 20, 30, 20, 20, 30, 30, 0, 0, 0, 0, 10, 40, 20, 52, 20, 40, 31, 52]),
    new Uint32Array([0, 0, 0, 1, 1]),
  ),
};

// Session.pages and text.layer answer at once; render.page never answers, which keeps the test to
// the view (the page images are not needed to select text). With `hold`, the text layer of that
// page answers only once `release` resolves (a page whose text has not arrived yet).
function fakeRpc(hold?: { page: number; release: Promise<void> }): PageRpc {
  const rpc = {
    call: vi.fn((method: string, params?: { page?: number }) => {
      if (method === 'session.pages') {
        return Promise.resolve({
          pages: Array.from({ length: 3 }, () => ({ width: 612, height: 792, rotation: 0 })),
          labels: [],
        });
      }
      if (method === 'text.layer') {
        if (hold !== undefined && params?.page === hold.page) {
          return hold.release.then(() => TEXT_LAYER);
        }
        return Promise.resolve(TEXT_LAYER);
      }
      return new Promise(() => {});
    }),
  };
  return rpc as unknown as PageRpc;
}

let host: HTMLDivElement;
let root: Root;
let handle = createRef<DocumentViewHandle>();

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function render(tool: ViewTool, rpc: PageRpc = fakeRpc()): void {
  act(() => {
    root.render(<DocumentView ref={handle} rpc={rpc} handle="h" tool={tool} />);
  });
}

beforeEach(async () => {
  window.localStorage.setItem(PREFS_STORAGE_KEY, JSON.stringify({ defaultZoom: 'actual' }));
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
  handle = createRef<DocumentViewHandle>();
  render('select');
  await flush();
  await flush();
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  window.getSelection()?.removeAllRanges();
});

// jsdom has no layout: page k is a box 800 by 1000 CSS px, the pages stacked one above the other, so
// the points of the layer (4/3 CSS px each at zoom 1) land on its lines.
function pageBox(index: number): HTMLElement {
  const page = host.querySelector<HTMLElement>(`.doc-page[data-page="${index}"]`);
  if (page === null) throw new Error(`no page ${index}`);
  const top = index * 1000;
  page.getBoundingClientRect = () =>
    ({ left: 0, top, right: 800, bottom: top + 1000, width: 800, height: 1000 }) as DOMRect;
  return page;
}

function pageZero(): HTMLElement {
  return pageBox(0);
}

function line(index: number): HTMLElement {
  const span = host.querySelectorAll<HTMLElement>('.doc-page[data-page="0"] .page-text-line')[
    index
  ];
  if (span === undefined) throw new Error(`no text line ${index}`);
  return span;
}

function pointer(element: HTMLElement | Window, type: string, x: number, y: number): void {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'mouse' } });
  act(() => {
    element.dispatchEvent(event);
  });
}

// A native selection over page 0's lines: from the start of line `from` to the end of line `to`.
function nativeSelect(from: number, to: number): Selection {
  const range = document.createRange();
  const start = line(from).firstChild;
  const end = line(to).firstChild;
  if (!(start instanceof Text) || !(end instanceof Text)) throw new Error('no text node');
  range.setStart(start, 0);
  range.setEnd(end, end.length);
  const selection = window.getSelection();
  if (selection === null) throw new Error('no selection');
  selection.removeAllRanges();
  selection.addRange(range);
  return selection;
}

// Copy as App wires it: the view's plan first (copyPlan), then the browser's selection when the model
// is off. A large selection goes to the engine, which this file does not run: its plan is recorded.
function copy(): {
  writeText: ReturnType<typeof vi.fn>;
  push: ReturnType<typeof vi.fn>;
  large: ReturnType<typeof vi.fn>;
} {
  const writeText = vi.fn(() => Promise.resolve());
  const push = vi.fn();
  const large = vi.fn();
  const store = { push } as unknown as ToastStore;
  copySelectedText({
    selection: window.getSelection(),
    lookup: (page) => handle.current?.cachedLines(page),
    clipboard: { writeText },
    store,
    plan: () => handle.current?.copyPlan() ?? { kind: 'off' },
    large,
  });
  return { writeText, push, large };
}

// A promise the test settles by hand (a page whose text layer answers when the test says so).
function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve: () => void = () => undefined;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

// A drag from the start of page 0's first line to the end of the last line of page `last`.
function dragTo(last: number, pointY: number): void {
  pageZero();
  pageBox(last);
  pointer(line(0), 'pointerdown', 10 * (4 / 3) + 1, 20 * (4 / 3) + 1);
  pointer(window, 'pointermove', 31 * (4 / 3), pointY);
  pointer(window, 'pointerup', 31 * (4 / 3), pointY);
}

describe('DocumentView copy with the Select tool (W5.4)', () => {
  it('copies a drag made under Select, then the native selection once Hand is chosen', async () => {
    // A press on "a" (10, 20 pt) and a drag to the end of "d" (31, 52 pt), 4/3 CSS px per point.
    dragTo(0, 52 * (4 / 3));
    expect(handle.current?.copyPlan()).toMatchObject({ kind: 'local', text: 'ab\ncd' });

    render('hand');
    await flush();
    expect(handle.current?.copyPlan()).toEqual({ kind: 'off' });
    nativeSelect(0, 0);
    expect(copy().writeText).toHaveBeenCalledWith('ab');
  });

  it('gives one mark area for page 0 after a drag (the selection Mark Text reads)', () => {
    dragTo(0, 52 * (4 / 3));
    const read = handle.current?.selectedAreas();
    expect(read?.state).toBe('ready');
    if (read?.state !== 'ready') return;
    expect(read.value).toHaveLength(1);
    expect(read.value[0]).toMatchObject({ page: 0, text: 'ab\ncd' });
  });

  it('copies the native selection, not a Select All left over after the tool changed', async () => {
    pageZero();
    act(() => handle.current?.selectAll());
    await flush();
    expect(handle.current?.selectedPageSpan()).toBe(3);

    render('hand');
    await flush();
    expect(handle.current?.copyPlan()).toEqual({ kind: 'off' });
    nativeSelect(1, 1);
    expect(copy().writeText).toHaveBeenCalledWith('cd');
  });

  it('Select All under Hand selects the page as a DOM range, and no model selection shows on return', async () => {
    render('hand');
    await flush();
    act(() => handle.current?.selectAll());
    expect(copy().writeText).toHaveBeenCalledWith('ab\ncd');

    render('select');
    await flush();
    expect(handle.current?.copyPlan()).toMatchObject({ kind: 'local', text: '' });
    expect(handle.current?.selectedPageSpan()).toBe(0);
  });

  it('Select All under Select covers the whole document, and says so', async () => {
    act(() => handle.current?.selectAll());
    await flush();
    expect(handle.current?.selectedPageSpan()).toBe(3);
    expect(toasts.getState().visible.map((toast) => toast.text)).toContain(
      'Selected the text of all 3 pages.',
    );
  });

  it('a drag made while Select All waits for the last page wins, and the late length is dropped', async () => {
    const gate = deferred();
    render('select', fakeRpc({ page: 2, release: gate.promise }));
    await flush();
    act(() => handle.current?.selectAll());
    expect(handle.current?.copyPlan()).toEqual(expect.objectContaining({ kind: 'pending' }));

    const first = copy();
    expect(first.writeText).not.toHaveBeenCalled();
    expect(first.push).toHaveBeenCalledWith('info', EN['text.stillLoading']);

    dragTo(0, 52 * (4 / 3));
    gate.resolve();
    await flush();
    await flush();
    expect(handle.current?.selectedPageSpan()).toBe(1);
    expect(handle.current?.copyPlan()).toMatchObject({ kind: 'local', text: 'ab\ncd' });
  });

  it('hands a selection with an unloaded page to the engine, not to the clipboard', async () => {
    // A selection over pages 0 to 2 right after the view mounts: the pages whose text has not
    // arrived are copied by the engine in slices, so the view's copy plan names the engine.
    dragTo(1, 1000 + 52 * (4 / 3));
    await flush();
    render('select', fakeRpc({ page: 1, release: deferred().promise }));
    await flush();
    await flush();
    expect(handle.current?.copyPlan()).toMatchObject({ kind: 'engine', pages: 2 });
    const first = copy();
    expect(first.large).toHaveBeenCalledTimes(1);
    expect(first.writeText).not.toHaveBeenCalled();
  });

  it('drops a model selection when a page it covers changes revision (Undo, Redo, an edit)', async () => {
    // The tab's scheduler is shared in through the scheduler prop, so the test moves a page's
    // revision the way a change reply does (App calls noteRevisions with the reply's revisions).
    const rpc = fakeRpc();
    const scheduler = new RenderScheduler(rpc, 'h');
    act(() => root.unmount());
    root = createRoot(host);
    act(() => {
      root.render(
        <DocumentView ref={handle} rpc={rpc} handle="h" tool="select" scheduler={scheduler} />,
      );
    });
    await flush();
    await flush();

    dragTo(0, 52 * (4 / 3));
    expect(handle.current?.copyPlan()).toMatchObject({ kind: 'local', text: 'ab\ncd' });

    act(() => {
      scheduler.noteRevisions({ '0': 2 });
    });
    expect(handle.current?.copyPlan()).toMatchObject({ kind: 'local', text: '' });
    expect(handle.current?.selectedAreas()).toEqual({ state: 'ready', value: [] });
  });

  it('clears a whole-document selection when a page of it changes (the last one here)', async () => {
    const rpc = fakeRpc();
    const scheduler = new RenderScheduler(rpc, 'h');
    act(() => root.unmount());
    root = createRoot(host);
    act(() => {
      root.render(
        <DocumentView ref={handle} rpc={rpc} handle="h" tool="select" scheduler={scheduler} />,
      );
    });
    await flush();
    await flush();

    act(() => handle.current?.selectAll());
    await flush();
    expect(handle.current?.selectedPageSpan()).toBe(3);
    act(() => {
      scheduler.noteRevisions({ '2': 5 });
    });
    expect(handle.current?.selectedPageSpan()).toBe(0);
  });

  it('clears the selection when the page count changes', async () => {
    dragTo(0, 52 * (4 / 3));
    expect(handle.current?.selectedPageSpan()).toBe(1);
    // A document of two pages instead of three: page numbers now name other pages.
    const twoPages: PageRpc = {
      call: vi.fn((method: string) => {
        if (method === 'session.pages') {
          return Promise.resolve({
            pages: Array.from({ length: 2 }, () => ({ width: 612, height: 792, rotation: 0 })),
            labels: [],
          });
        }
        return new Promise(() => {});
      }),
    } as unknown as PageRpc;
    render('select', twoPages);
    await flush();
    await flush();
    expect(handle.current?.selectedPageSpan()).toBe(0);
  });
});
