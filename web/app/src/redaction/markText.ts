// Mark Text (W4.12 S2): marks the text selection as redaction marks, one per page, in one undo step.
// The model's selection (a drag, or Select All) is read first; the browser's is read only when the
// model is off (the Hand tool). Nothing is applied: the marks wait in the document until the user
// applies them, as on the desktop. The command is enabled with no selection, and then says so.
import { toasts, type ToastStore } from '../feedback/toasts';
import { t, tp } from '../i18n/t';
import { domSelectionAreas, type ModelRead, type TextLineLookup } from '../text/selection';
import { MAX_TEXT_LAYERS } from '../text/textLayerCache';
import type { RedactionSnapshot } from './controller';
import type { PageArea } from '../text/textLayer';

export interface MarkTextDeps {
  // The number of pages the model's selection covers (0 when there is none). Mark Text works on at
  // most MAX_TEXT_LAYERS pages, since it needs each covered page's areas (W5.4 section 6).
  readonly pageSpan?: () => number;
  readonly controller: {
    getSnapshot(): Pick<RedactionSnapshot, 'busy'>;
    markAreas(areas: readonly PageArea[]): Promise<number>;
  };
  // The model's selection areas (the view's selectedAreas). `off` reads the browser's selection.
  readonly model: () => ModelRead<PageArea[]>;
  // Clears the model's selection once its areas are marked (the view's clearTextSelection).
  readonly clearModel: () => void;
  // Asks for the indexes a `loading` read names, and resolves once they have been asked for.
  readonly loadPages: (pages: readonly number[]) => Promise<void>;
  // The window's selection, or null when there is none to read (the browser path only).
  readonly selection: Selection | null;
  // The text lines of a page as they are shown (the view's cachedLines), for the browser path.
  readonly lookup: TextLineLookup;
  readonly store?: Pick<ToastStore, 'push'>;
}

// Marks the selected text. Ignored while an apply runs. The selection is cleared once it is marked,
// so a second press does not mark the same text again. A `loading` selection waits for its pages
// (Mark Text has no user-activation limit), then reads the selection again: a selection the user has
// dropped in the meantime is not marked.
export async function markSelectedText(deps: MarkTextDeps): Promise<void> {
  const store = deps.store ?? toasts;
  if (deps.controller.getSnapshot().busy) return;
  if ((deps.pageSpan?.() ?? 0) > MAX_TEXT_LAYERS) {
    store.push('info', t('redact.selectionTooLarge'));
    return;
  }
  let read = deps.model();
  if (read.state === 'loading') {
    await deps.loadPages(read.pages);
    if (deps.controller.getSnapshot().busy) return;
    read = deps.model();
    if (read.state === 'loading') {
      store.push('info', t('text.stillLoading'));
      return;
    }
    if (read.state === 'off') return;
  }
  if (read.state === 'ready') {
    if (read.value.length === 0) {
      store.push('info', t('redact.selectTextFirst'));
      return;
    }
    const added = await deps.controller.markAreas(read.value);
    if (added === 0) return;
    deps.clearModel();
    store.push('success', tp('redact.markedText', added));
    return;
  }
  const areas = domSelectionAreas(deps.selection, deps.lookup);
  if (areas.length === 0) {
    store.push('info', t('redact.selectTextFirst'));
    return;
  }
  const added = await deps.controller.markAreas(areas);
  if (added === 0) return;
  deps.selection?.removeAllRanges();
  store.push('success', tp('redact.markedText', added));
}
