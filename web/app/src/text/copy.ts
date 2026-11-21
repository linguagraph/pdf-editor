// Copy and Select All over the page text (W5.4 S4, W1.4 section 10). The model's selection (a drag,
// or Select All) is read first: its chars are the text copied, the chars of each page in reading
// order, lines joined by a line break, as the desktop copies. Pages are joined the same way. The
// browser's selection is read only when the model is off (the Hand tool, see DocumentView). A
// selection whose pages are not all loaded is copied by the engine in slices (W5.4, largeCopy.ts).
// Nothing here changes the document.
import { toasts, type ToastStore } from '../feedback/toasts';
import { t } from '../i18n/t';
import { LINE_SEP, type PageTextIndex } from './pageTextIndex';
import { type EnginePlan, MAX_COPY_CHARS } from './largeCopy';
import { domSelectionAreas, type ModelRead, type TextLineLookup } from './selection';
import { type PageIndexes, type TextSelection } from './textSelection';

export { MAX_COPY_CHARS } from './largeCopy';

// What Copy does with the model's selection (W5.4 section 3.1). `off`: Hand or no view, so the
// browser's selection is copied. `pending`: Select All still waits for the last page's length.
// `local`: every covered index is loaded, so the text is built here ('' for nothing selected).
// `engine`: some are not, so the engine copies the text in slices.
export type CopyPlan =
  | { readonly kind: 'off' }
  | { readonly kind: 'pending' }
  | { readonly kind: 'local'; readonly text: string }
  | ({ readonly kind: 'engine' } & EnginePlan);

export interface CopyDeps {
  // The window's selection, or null when there is none to read.
  readonly selection: Selection | null;
  // The text lines of a page as they are shown (the view's cachedLines).
  readonly lookup: TextLineLookup;
  readonly clipboard: Pick<Clipboard, 'writeText'> | undefined;
  readonly store?: Pick<ToastStore, 'push'>;
  // What the view's selection needs (DocumentView.copyPlan). Absent: the browser's selection.
  readonly plan?: () => CopyPlan;
  // Copies an `engine` plan in slices (largeCopy.copyLarge). It runs before copySelectedText returns,
  // so the clipboard call is still in the keypress.
  readonly large?: (plan: EnginePlan) => void;
}

// The copy plan of the model's selection. Pure: the view passes what it knows. `selecting` is false
// under Hand (or another tool that takes the drag), where the browser's selection is copied.
export function planCopy(
  selecting: boolean,
  selection: TextSelection | null,
  indexOf: (page: number) => PageTextIndex | undefined,
  pending: boolean,
  changes: EnginePlan['changes'],
): CopyPlan {
  if (!selecting) return { kind: 'off' };
  if (pending) return { kind: 'pending' };
  if (selection === null || selection.isEmpty) return { kind: 'local', text: '' };
  const read = readModelIndexes(selection, indexOf);
  if (read.state === 'ready') return { kind: 'local', text: selection.text(read.value) };
  return {
    kind: 'engine',
    start: selection.start,
    end: selection.end,
    pages: selection.end.page - selection.start.page + 1,
    changes,
  };
}

// The selected text, or '' when nothing selected has text. Each page's selected lines are joined by
// a line break, and so are the pages (the desktop's TextSelection.text).
export function selectedText(selection: Selection | null, lookup: TextLineLookup): string {
  return domSelectionAreas(selection, lookup)
    .map((area) => area.text)
    .join(LINE_SEP);
}

// The indexes of every page a model selection covers, or the pages whose index has not arrived. A
// page's index is looked up once; the model never copies part of a selection (decision D4).
export function readModelIndexes(
  selection: TextSelection,
  indexOf: (page: number) => PageTextIndex | undefined,
): ModelRead<PageIndexes> {
  const found = new Map<number, PageTextIndex>();
  const missing: number[] = [];
  for (let page = selection.start.page; page <= selection.end.page; page += 1) {
    const index = indexOf(page);
    if (index === undefined) missing.push(page);
    else found.set(page, index);
  }
  if (missing.length > 0) return { state: 'loading', pages: missing };
  return {
    state: 'ready',
    value: {
      get: (page) => {
        const index = found.get(page);
        if (index === undefined) throw new Error(`no index for page ${page}`);
        return index;
      },
    },
  };
}

// The text of the model's selection, pages joined as the desktop joins them. `ready` with '' when
// nothing is selected (no selection, or an empty one); `loading` when a covered page has no index.
export function readModelText(
  selection: TextSelection | null,
  indexOf: (page: number) => PageTextIndex | undefined,
): ModelRead<string> {
  if (selection === null || selection.isEmpty) return { state: 'ready', value: '' };
  const read = readModelIndexes(selection, indexOf);
  if (read.state !== 'ready') return read;
  return { state: 'ready', value: selection.text(read.value) };
}

// Copies the selection to the clipboard. It runs synchronously up to the clipboard call, so the
// browser still counts it as the user's keypress or click (Firefox and Safari require that).
export function copySelectedText(deps: CopyDeps): void {
  const store = deps.store ?? toasts;
  const plan: CopyPlan = deps.plan?.() ?? { kind: 'off' };
  if (plan.kind === 'pending') {
    store.push('info', t('text.stillLoading'));
    return;
  }
  if (plan.kind === 'engine') {
    if (deps.large === undefined) store.push('error', t('copy.failed'));
    else deps.large(plan);
    return;
  }
  const text = plan.kind === 'local' ? plan.text : selectedText(deps.selection, deps.lookup);
  if (text === '') {
    store.push('info', t('copy.nothingSelected'));
    return;
  }
  if (text.length > MAX_COPY_CHARS) {
    store.push('info', t('copy.tooLarge'));
    return;
  }
  if (deps.clipboard === undefined) {
    store.push('error', t('copy.failed'));
    return;
  }
  deps.clipboard.writeText(text).then(
    () => store.push('success', t('copy.done')),
    () => store.push('error', t('copy.failed')),
  );
}

// Selects everything in one page's text layer, so Copy and Mark Text read it as they would a drag
// over the page. A page with no text layer (or no selection to set) selects nothing.
export function selectPageText(selection: Selection | null, layer: Element | null): void {
  if (selection === null || layer === null) return;
  const range = layer.ownerDocument.createRange();
  range.selectNodeContents(layer);
  selection.removeAllRanges();
  selection.addRange(range);
}
