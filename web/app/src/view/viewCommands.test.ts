import { describe, expect, it, vi } from 'vitest';
import { REGISTRY } from '../commands/catalog';
import { commandDisabledReason, runCommand, type CommandHandlers } from '../commands/run';
import type { CommandState } from '../commands/registry';
import type { DocumentViewHandle } from './DocumentView';
import { runViewCommand, type ViewCommandId } from './viewCommands';

const VIEW_IDS: readonly ViewCommandId[] = [
  'zoom-in',
  'zoom-out',
  'fit-width',
  'fit-page',
  'actual-size',
  'previous-page',
  'next-page',
];

// The two history commands (Back, Forward) run from the app, not from the view handle.
const HISTORY_IDS = ['back', 'forward'] as const;

const STATE_WITH_DOCUMENT: CommandState = {
  hasDocument: true,
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
};
const STATE_NO_DOCUMENT: CommandState = { ...STATE_WITH_DOCUMENT, hasDocument: false };

function fakeView(): DocumentViewHandle {
  return {
    scrollToPage: vi.fn(),
    zoom: vi.fn(),
    stepPage: vi.fn(),
    focusPageField: vi.fn(),
    deleteEditSelection: vi.fn(),
    setLayout: vi.fn(),
    setNight: vi.fn(),
    cachedLines: vi.fn(() => undefined),
    setCommentTool: vi.fn(),
    setRedacting: vi.fn(),
    selectAll: vi.fn(),
    copyPlan: vi.fn(() => ({ kind: 'off' }) as const),
    selectedPageSpan: vi.fn(() => 0),
    selectedAreas: vi.fn(() => ({ state: 'off' }) as const),
    clearTextSelection: vi.fn(),
    loadTextPages: vi.fn(() => Promise.resolve()),
    revealRect: vi.fn(),
  };
}

// The handlers the app gives the registry for these commands, with the view and the history fakes.
function handlersFor(view: DocumentViewHandle): CommandHandlers {
  const handlers: Record<string, () => void> = {};
  for (const id of VIEW_IDS) handlers[id] = () => runViewCommand(id, view);
  for (const id of HISTORY_IDS) handlers[id] = vi.fn();
  return handlers;
}

describe('runViewCommand', () => {
  it('sends each zoom command to the view handle', () => {
    const view = fakeView();
    runViewCommand('zoom-in', view);
    runViewCommand('zoom-out', view);
    runViewCommand('fit-width', view);
    runViewCommand('fit-page', view);
    runViewCommand('actual-size', view);
    expect(view.zoom).toHaveBeenNthCalledWith(1, { step: 1 });
    expect(view.zoom).toHaveBeenNthCalledWith(2, { step: -1 });
    expect(view.zoom).toHaveBeenNthCalledWith(3, { fit: 'width' });
    expect(view.zoom).toHaveBeenNthCalledWith(4, { fit: 'page' });
    expect(view.zoom).toHaveBeenNthCalledWith(5, { zoom: 1 });
  });

  it('steps the page for previous and next page', () => {
    const view = fakeView();
    runViewCommand('previous-page', view);
    runViewCommand('next-page', view);
    expect(view.stepPage).toHaveBeenNthCalledWith(1, -1);
    expect(view.stepPage).toHaveBeenNthCalledWith(2, 1);
  });
});

describe('the nine navigation commands in the registry', () => {
  it('are disabled with the needs-a-document reason when no document is open', () => {
    const handlers = handlersFor(fakeView());
    for (const id of [...VIEW_IDS, ...HISTORY_IDS]) {
      const def = REGISTRY.get(id);
      expect(def, id).toBeDefined();
      if (def === undefined) continue;
      expect(commandDisabledReason(def, STATE_NO_DOCUMENT, handlers), id).toBe('needsDocument');
      expect(runCommand(REGISTRY, id, STATE_NO_DOCUMENT, handlers), id).toBe(false);
    }
  });

  it('run through the registry and reach the view when a document is open', () => {
    const view = fakeView();
    const handlers = handlersFor(view);
    for (const id of VIEW_IDS) {
      expect(runCommand(REGISTRY, id, STATE_WITH_DOCUMENT, handlers), id).toBe(true);
    }
    // Back and Forward run only when the history has an entry that way (W4.3a S6).
    const withHistory = { ...STATE_WITH_DOCUMENT, canGoBack: true, canGoForward: true };
    for (const id of HISTORY_IDS) {
      expect(runCommand(REGISTRY, id, withHistory, handlers), id).toBe(true);
    }
    expect(view.zoom).toHaveBeenCalledTimes(5);
    expect(view.stepPage).toHaveBeenCalledTimes(2);
  });
});
