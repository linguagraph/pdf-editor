import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createToastStore } from '../feedback/toasts';
import { EN } from '../i18n/en';
import { t, tp } from '../i18n/t';
import type { PageArea } from '../text/textLayer';
import { domSelectionAreas, type ModelRead } from '../text/selection';
import { markSelectedText, type MarkTextDeps } from './markText';

vi.mock('../text/selection', () => ({ domSelectionAreas: vi.fn(() => [] as PageArea[]) }));

const AREA: PageArea = {
  page: 0,
  rect: { x0: 1, y0: 2, x1: 30, y1: 40 },
  quads: [],
  text: 'jane',
};

function fakeController(busy = false, added = 1) {
  return {
    getSnapshot: () => ({ busy }),
    markAreas: vi.fn(async () => added),
  };
}

function fakeSelection() {
  return { removeAllRanges: vi.fn() } as unknown as Selection & {
    removeAllRanges: ReturnType<typeof vi.fn>;
  };
}

// The deps of a press with the model off (the browser's selection is the one read) unless given.
function deps(overrides: Partial<MarkTextDeps> = {}): MarkTextDeps {
  return {
    controller: fakeController(),
    model: () => ({ state: 'off' }),
    clearModel: vi.fn(),
    loadPages: vi.fn(async () => undefined),
    selection: null,
    lookup: () => undefined,
    store: createToastStore(),
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(domSelectionAreas).mockReturnValue([]);
});

describe('markSelectedText (W4.12 S2)', () => {
  it('shows an info message and marks nothing with no selected text', async () => {
    const store = createToastStore();
    const controller = fakeController();
    await markSelectedText(deps({ controller, store }));
    expect(controller.markAreas).not.toHaveBeenCalled();
    const [toast] = store.getState().visible;
    expect(toast?.kind).toBe('info');
    expect(toast?.text).toBe(t('redact.selectTextFirst'));
  });

  it('marks the browser selection areas under Hand, clears the selection and says how many', async () => {
    vi.mocked(domSelectionAreas).mockReturnValue([AREA]);
    const store = createToastStore();
    const controller = fakeController(false, 1);
    const selection = fakeSelection();
    await markSelectedText(deps({ controller, selection, store }));
    expect(controller.markAreas).toHaveBeenCalledWith([AREA]);
    expect(selection.removeAllRanges).toHaveBeenCalledTimes(1);
    const [toast] = store.getState().visible;
    expect(toast?.kind).toBe('success');
    expect(toast?.text).toBe(tp('redact.markedText', 1));
  });

  it('keeps the selection and says nothing when the mark added nothing', async () => {
    vi.mocked(domSelectionAreas).mockReturnValue([AREA]);
    const store = createToastStore();
    const selection = fakeSelection();
    await markSelectedText(deps({ controller: fakeController(false, 0), selection, store }));
    expect(selection.removeAllRanges).not.toHaveBeenCalled();
    expect(store.getState().visible).toHaveLength(0);
  });

  it('does nothing while an apply runs', async () => {
    vi.mocked(domSelectionAreas).mockReturnValue([AREA]);
    const controller = fakeController(true);
    await markSelectedText(deps({ controller, selection: fakeSelection() }));
    expect(controller.markAreas).not.toHaveBeenCalled();
  });
});

describe('markSelectedText on the model selection (W5.4)', () => {
  it('marks the model areas, clears the model and not the browser ranges', async () => {
    const store = createToastStore();
    const controller = fakeController(false, 1);
    const clearModel = vi.fn();
    const selection = fakeSelection();
    await markSelectedText(
      deps({ controller, clearModel, selection, store, model: () => ready([AREA]) }),
    );
    expect(controller.markAreas).toHaveBeenCalledWith([AREA]);
    expect(clearModel).toHaveBeenCalledTimes(1);
    expect(selection.removeAllRanges).not.toHaveBeenCalled();
    expect(domSelectionAreas).not.toHaveBeenCalled();
    expect(store.getState().visible[0]?.text).toBe(tp('redact.markedText', 1));
  });

  it('shows selectTextFirst for an empty model selection, without reading the browser', async () => {
    const store = createToastStore();
    const controller = fakeController();
    await markSelectedText(deps({ controller, store, model: () => ready([]) }));
    expect(controller.markAreas).not.toHaveBeenCalled();
    expect(store.getState().visible[0]?.text).toBe(t('redact.selectTextFirst'));
    expect(domSelectionAreas).not.toHaveBeenCalled();
  });

  it('waits for loading pages, then marks the selection read again', async () => {
    const store = createToastStore();
    const controller = fakeController(false, 1);
    const clearModel = vi.fn();
    const loadPages = vi.fn(async () => undefined);
    let reads = 0;
    const model = (): ModelRead<PageArea[]> => {
      reads += 1;
      return reads === 1 ? { state: 'loading', pages: [1] } : ready([AREA]);
    };
    await markSelectedText(deps({ controller, clearModel, loadPages, store, model }));
    expect(loadPages).toHaveBeenCalledWith([1]);
    expect(controller.markAreas).toHaveBeenCalledWith([AREA]);
    expect(clearModel).toHaveBeenCalledTimes(1);
  });

  it('marks nothing and says the text is still loading when a page stays unready', async () => {
    const store = createToastStore();
    const controller = fakeController();
    await markSelectedText(
      deps({ controller, store, model: () => ({ state: 'loading', pages: [1] }) }),
    );
    expect(controller.markAreas).not.toHaveBeenCalled();
    expect(store.getState().visible[0]?.text).toBe(t('text.stillLoading'));
  });

  it('marks nothing when the user has dropped the selection while the pages loaded', async () => {
    const controller = fakeController();
    let reads = 0;
    const model = (): ModelRead<PageArea[]> => {
      reads += 1;
      return reads === 1 ? { state: 'loading', pages: [1] } : { state: 'off' };
    };
    await markSelectedText(deps({ controller, model }));
    expect(controller.markAreas).not.toHaveBeenCalled();
  });

  it('does nothing while an apply runs, before any read', async () => {
    const controller = fakeController(true);
    const model = vi.fn(() => ready([AREA]));
    await markSelectedText(deps({ controller, model }));
    expect(model).not.toHaveBeenCalled();
    expect(controller.markAreas).not.toHaveBeenCalled();
  });

  it('refuses a selection over more than 300 pages, with a message, and marks nothing (W5.4 section 6)', async () => {
    const store = createToastStore();
    const controller = fakeController(false, 1);
    const loadPages = vi.fn(async () => undefined);
    const model = vi.fn(() => ready([AREA]));
    await markSelectedText(deps({ controller, store, loadPages, model, pageSpan: () => 301 }));
    expect(store.getState().visible.map((toast) => toast.text)).toContain(
      EN['redact.selectionTooLarge'],
    );
    expect(loadPages).not.toHaveBeenCalled();
    expect(model).not.toHaveBeenCalled();
    expect(controller.markAreas).not.toHaveBeenCalled();
  });

  it('marks a selection of exactly 300 pages', async () => {
    const controller = fakeController(false, 1);
    await markSelectedText(deps({ controller, model: () => ready([AREA]), pageSpan: () => 300 }));
    expect(controller.markAreas).toHaveBeenCalledWith([AREA]);
  });
});

function ready(value: PageArea[]): ModelRead<PageArea[]> {
  return { state: 'ready', value };
}
