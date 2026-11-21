import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ToastStore } from '../feedback/toasts';
import { EN } from '../i18n/en';
import { COMMANDS, REGISTRY } from '../commands/catalog';
import { commandForEvent } from '../commands/shortcuts';
import { commandDisabledReason, runCommand, type CommandHandlers } from '../commands/run';
import type { CommandState } from '../commands/registry';
import { copySelectedText, planCopy, readModelText, selectPageText, selectedText } from './copy';
import { PageTextIndex } from './pageTextIndex';
import { type TextLineLookup, domSelectionAreas } from './selection';
import { decodeTextLayer, type TextLine } from './textLayer';
import { TextSelection } from './textSelection';

// Page 0: "ab" and "cd", two lines. Page 1: "ef", one line (the layers of selection.test.ts).
const LAYER_0 = {
  text: 'ab\ncd',
  char_boxes: new Float32Array([
    10, 20, 20, 30, 20, 20, 30, 30, 0, 0, 0, 0, 10, 40, 20, 52, 20, 40, 31, 52,
  ]).buffer,
  line_numbers: new Uint32Array([0, 0, 0, 1, 1]).buffer,
};
const LAYER_1 = {
  text: 'ef',
  char_boxes: new Float32Array([100, 100, 110, 110, 110, 100, 120, 110]).buffer,
  line_numbers: new Uint32Array([0, 0]).buffer,
};
const LINES_0 = decodeTextLayer(LAYER_0);
const LINES_1 = decodeTextLayer(LAYER_1);
const lookup: TextLineLookup = (page) => (page === 0 ? LINES_0 : page === 1 ? LINES_1 : undefined);

// The page elements as PageTextLayer renders them: one span per line inside the page's text layer.
function mountPage(index: number, lines: readonly TextLine[]): HTMLElement {
  const page = document.createElement('div');
  page.className = 'doc-page';
  page.dataset.page = String(index);
  const layer = document.createElement('div');
  layer.className = 'page-text-layer';
  lines.forEach((line, lineIndex) => {
    const span = document.createElement('div');
    span.className = 'page-text-line';
    span.dataset.line = String(lineIndex);
    span.textContent = line.text;
    layer.append(span);
  });
  page.append(layer);
  document.body.append(page);
  return page;
}

function textOf(page: HTMLElement, lineIndex: number): Text {
  const text = page.querySelectorAll('.page-text-line')[lineIndex]?.firstChild;
  if (!(text instanceof Text)) throw new Error('no text node');
  return text;
}

function select(startNode: Node, startOffset: number, endNode: Node, endOffset: number): Selection {
  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);
  const selection = window.getSelection();
  if (selection === null) throw new Error('no selection');
  selection.removeAllRanges();
  selection.addRange(range);
  return selection;
}

function fakeStore(): ToastStore & { push: ReturnType<typeof vi.fn> } {
  return { push: vi.fn() } as unknown as ToastStore & { push: ReturnType<typeof vi.fn> };
}

function state(overrides: Partial<CommandState> = {}): CommandState {
  return {
    hasDocument: false,
    hasRecentFiles: false,
    canUndo: false,
    canRedo: false,
    capabilities: new Set(),
    contentEdit: false,
    ribbonCompact: false,
    ribbonCollapsed: false,
    viewTool: 'select',
    commentTool: null,
    redacting: false,
    canApplyRedactions: false,
    canGoBack: false,
    canGoForward: false,
    view: null,
    ...overrides,
  };
}

afterEach(() => {
  window.getSelection()?.removeAllRanges();
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});

describe('selectedText', () => {
  it('joins the lines of a page with a line break', () => {
    const page = mountPage(0, LINES_0);
    // From the "b" of "ab" to the "c" of "cd": "b", a line break, then "c".
    const selection = select(textOf(page, 0), 1, textOf(page, 1), 1);
    expect(selectedText(selection, lookup)).toBe('b\nc');
  });

  it('copies a selection across two pages, the pages joined by a line break', () => {
    const first = mountPage(0, LINES_0);
    const second = mountPage(1, LINES_1);
    const selection = select(textOf(first, 1), 0, textOf(second, 0), 1);
    expect(selectedText(selection, lookup)).toBe('cd\ne');
  });

  it('gives no text for an empty selection, a null selection or a page without lines', () => {
    const page = mountPage(0, LINES_0);
    expect(selectedText(select(textOf(page, 0), 1, textOf(page, 0), 1), lookup)).toBe('');
    expect(selectedText(null, lookup)).toBe('');
    const selection = select(textOf(page, 0), 0, textOf(page, 0), 2);
    expect(selectedText(selection, () => undefined)).toBe('');
  });
});

describe('copySelectedText', () => {
  it('writes the selected text to the clipboard and confirms it', async () => {
    const page = mountPage(0, LINES_0);
    const selection = select(textOf(page, 0), 0, textOf(page, 1), 2);
    const writeText = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copySelectedText({ selection, lookup, clipboard: { writeText }, store });
    expect(writeText).toHaveBeenCalledWith('ab\ncd');
    await vi.waitFor(() => expect(store.push).toHaveBeenCalledWith('success', EN['copy.done']));
  });

  it('writes synchronously, so the browser still counts the keypress as user activation', () => {
    const page = mountPage(0, LINES_0);
    const selection = select(textOf(page, 0), 0, textOf(page, 0), 2);
    const writeText = vi.fn(() => Promise.resolve());
    copySelectedText({ selection, lookup, clipboard: { writeText }, store: fakeStore() });
    expect(writeText).toHaveBeenCalledTimes(1);
  });

  it('says so, through the message catalogue, when nothing is selected', () => {
    const page = mountPage(0, LINES_0);
    const writeText = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copySelectedText({
      selection: select(textOf(page, 0), 1, textOf(page, 0), 1),
      lookup,
      clipboard: { writeText },
      store,
    });
    expect(writeText).not.toHaveBeenCalled();
    expect(store.push).toHaveBeenCalledWith('info', EN['copy.nothingSelected']);
  });

  it('reports a clipboard that refuses the text', async () => {
    const page = mountPage(0, LINES_0);
    const selection = select(textOf(page, 0), 0, textOf(page, 0), 2);
    const writeText = vi.fn(() => Promise.reject(new Error('denied')));
    const store = fakeStore();
    copySelectedText({ selection, lookup, clipboard: { writeText }, store });
    await vi.waitFor(() => expect(store.push).toHaveBeenCalledWith('error', EN['copy.failed']));
  });
});

// The model's index of page 0 ("ab" and "cd", as useTextSelection.test.tsx lays them out).
const MODEL_INDEX = new PageTextIndex({
  text: 'ab\ncd',
  lineRects: [
    { x0: 72, y0: 72, x1: 84, y1: 84 },
    { x0: 72, y0: 84, x1: 84, y1: 96 },
  ],
  charBoxes: [72, 72, 78, 84, 78, 72, 84, 84, 0, 0, 0, 0, 72, 84, 78, 96, 78, 84, 84, 96],
  lineNumbers: [0, 0, 0, 1, 1],
});
const indexOf = (page: number) => (page === 0 ? MODEL_INDEX : undefined);
// A change listener that never hears anything (a plan that is not run in these tests).
const noChanges = () => () => {};

describe('planCopy (W5.4 section 3.1)', () => {
  const two = new TextSelection({ page: 0, index: 0 }, { page: 1, index: 2 });

  it('is off when the model is not selecting (Hand)', () => {
    expect(planCopy(false, two, indexOf, false, noChanges)).toEqual({ kind: 'off' });
  });

  it('is pending while Select All waits for the last page', () => {
    expect(planCopy(true, two, indexOf, true, noChanges)).toEqual({ kind: 'pending' });
  });

  it('is local with an empty text for no selection or an empty one', () => {
    expect(planCopy(true, null, indexOf, false, noChanges)).toEqual({ kind: 'local', text: '' });
    const empty = new TextSelection({ page: 0, index: 1 }, { page: 0, index: 1 });
    expect(planCopy(true, empty, indexOf, false, noChanges)).toEqual({ kind: 'local', text: '' });
  });

  it('is local with the text when every covered index is loaded', () => {
    const one = new TextSelection({ page: 0, index: 0 }, { page: 0, index: 5 });
    expect(planCopy(true, one, indexOf, false, noChanges)).toEqual({
      kind: 'local',
      text: 'ab\ncd',
    });
  });

  it('is engine, with the ends and the page count, when a covered index is not loaded', () => {
    const plan = planCopy(true, two, indexOf, false, noChanges);
    expect(plan).toMatchObject({
      kind: 'engine',
      start: { page: 0, index: 0 },
      end: { page: 1, index: 2 },
      pages: 2,
    });
  });
});

describe('model selection read (W5.4: a drag or Select All)', () => {
  it('reads the model selection of two lines as "ab\\ncd"', () => {
    const selection = new TextSelection({ page: 0, index: 0 }, { page: 0, index: 5 });
    expect(readModelText(selection, indexOf)).toEqual({ state: 'ready', value: 'ab\ncd' });
  });

  it('is ready with an empty text for no selection or an empty one, whatever the index', () => {
    expect(readModelText(null, () => undefined)).toEqual({ state: 'ready', value: '' });
    const empty = new TextSelection({ page: 0, index: 2 }, { page: 0, index: 2 });
    expect(readModelText(empty, () => undefined)).toEqual({ state: 'ready', value: '' });
  });

  it('is loading, naming the pages, when a covered page has no index yet', () => {
    const selection = new TextSelection({ page: 0, index: 0 }, { page: 2, index: 1 });
    expect(readModelText(selection, indexOf)).toEqual({ state: 'loading', pages: [1, 2] });
  });

  it('copies the model text when the browser has no selection', () => {
    const writeText = vi.fn(() => Promise.resolve());
    const selection = new TextSelection({ page: 0, index: 0 }, { page: 0, index: 5 });
    copySelectedText({
      selection: null,
      lookup,
      clipboard: { writeText },
      store: fakeStore(),
      plan: () => planCopy(true, selection, indexOf, false, noChanges),
    });
    expect(writeText).toHaveBeenCalledWith('ab\ncd');
  });

  it('copies the browser selection when the model is off (Hand)', () => {
    const page = mountPage(0, LINES_0);
    const writeText = vi.fn(() => Promise.resolve());
    copySelectedText({
      selection: select(textOf(page, 0), 0, textOf(page, 0), 2),
      lookup,
      clipboard: { writeText },
      store: fakeStore(),
      plan: () => ({ kind: 'off' }),
    });
    expect(writeText).toHaveBeenCalledWith('ab');
  });

  it('does not fall back to the browser when the model is local and empty', () => {
    const page = mountPage(0, LINES_0);
    const writeText = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copySelectedText({
      selection: select(textOf(page, 0), 0, textOf(page, 0), 2),
      lookup,
      clipboard: { writeText },
      store,
      plan: () => ({ kind: 'local', text: '' }),
    });
    expect(writeText).not.toHaveBeenCalled();
    expect(store.push).toHaveBeenCalledWith('info', EN['copy.nothingSelected']);
  });

  it('says the text is still loading while Select All waits, without a clipboard call', () => {
    const writeText = vi.fn(() => Promise.resolve());
    const store = fakeStore();
    copySelectedText({
      selection: null,
      lookup,
      clipboard: { writeText },
      store,
      plan: () => ({ kind: 'pending' }),
    });
    expect(writeText).not.toHaveBeenCalled();
    expect(store.push).toHaveBeenCalledWith('info', EN['text.stillLoading']);
  });

  it('hands an engine plan to the large copy, before it returns', () => {
    const large = vi.fn();
    const plan = {
      kind: 'engine' as const,
      start: { page: 0, index: 0 },
      end: { page: 2, index: 1 },
      pages: 3,
      changes: noChanges,
    };
    const writeText = vi.fn(() => Promise.resolve());
    copySelectedText({
      selection: null,
      lookup,
      clipboard: { writeText },
      store: fakeStore(),
      plan: () => plan,
      large,
    });
    expect(large).toHaveBeenCalledWith(plan);
    expect(writeText).not.toHaveBeenCalled();
  });

  it('Select All selects the whole page in the model, and Copy yields it', () => {
    const selection = TextSelection.ofPage(0, MODEL_INDEX.length);
    expect(readModelText(selection, indexOf)).toEqual({ state: 'ready', value: 'ab\ncd' });
  });
});

describe('selectPageText (Select All)', () => {
  it('selects the whole text layer of the page, so the copy reads all of its lines', () => {
    const page = mountPage(0, LINES_0);
    const layer = page.querySelector('.page-text-layer');
    selectPageText(window.getSelection(), layer);
    expect(selectedText(window.getSelection(), lookup)).toBe('ab\ncd');
    expect(domSelectionAreas(window.getSelection(), lookup)).toHaveLength(1);
  });

  it('selects nothing when the page has no text layer', () => {
    const selection = window.getSelection();
    selectPageText(selection, null);
    expect(selectedText(selection, lookup)).toBe('');
  });
});

describe('Copy and Select All enable rules', () => {
  const handlers: CommandHandlers = { copy: vi.fn(), 'select-all': vi.fn() };

  it('need an open document and a handler', () => {
    for (const id of ['copy', 'select-all']) {
      const def = REGISTRY.get(id);
      expect(def).toBeDefined();
      if (def === undefined) continue;
      expect(commandDisabledReason(def, state(), handlers)).toBe('needsDocument');
      expect(commandDisabledReason(def, state({ hasDocument: true }), handlers)).toBeNull();
      expect(commandDisabledReason(def, state({ hasDocument: true }), {})).toBe('notAvailableYet');
    }
    expect(COMMANDS.some((def) => def.id === 'copy')).toBe(true);
  });

  it('run through runCommand only with a document', () => {
    const run = vi.fn();
    const local: CommandHandlers = { copy: run };
    expect(runCommand(REGISTRY, 'copy', state(), local)).toBe(false);
    expect(run).not.toHaveBeenCalled();
    expect(runCommand(REGISTRY, 'copy', state({ hasDocument: true }), local)).toBe(true);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('bind Ctrl+C and Ctrl+A to the commands', () => {
    const ctrl = (key: string) => ({
      key,
      code: `Key${key.toUpperCase()}`,
      ctrlKey: true,
      metaKey: false,
      shiftKey: false,
      altKey: false,
    });
    const open = state({ hasDocument: true });
    expect(commandForEvent(ctrl('c'), REGISTRY, open, false)).toBe('copy');
    expect(commandForEvent(ctrl('a'), REGISTRY, open, false)).toBe('select-all');
  });
});
