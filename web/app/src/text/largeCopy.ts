// Copies a large selection in slices (W5.4, W1.4 section 10). The engine's text.extract gives the
// text one slice of pages at a time, each within about COPY_BUDGET_MS, so the engine worker is busy
// for one slice at a time and Cancel waits at most for the slice in flight. The slices are joined
// as the desktop joins pages (LINE_SEP between non-empty pieces), and the clipboard gets the text
// as a Blob built from the pieces, so the whole text never exists as one JS string before the write.
//
// Firefox and Safari take a clipboard write only inside the keypress, so the write starts at once
// with a promise for the data (ClipboardItem). Where that is refused, the text is kept and the next
// press (the toast's button, or Copy again on the same selection) writes it.
import type { PageRpc } from '../engine/engineConnection';
import { jobProgress, type JobProgressStore } from '../feedback/jobRun';
import { reportRpcError, type ToastStore } from '../feedback/toasts';
import { t } from '../i18n/t';
import { RpcError } from '../rpc/client';
import { LINE_SEP } from './pageTextIndex';
import type { TextPos } from './textSelection';

// The most text one copy takes (W1.4 section 10, decision D4): a larger selection is refused with a
// message, never cut short, so the clipboard never gets part of what was selected.
export const MAX_COPY_CHARS = 50 * 1024 * 1024;
// The engine's time per slice (W1.4 section 10), as search uses.
export const COPY_BUDGET_MS = 100;
// The bar shows only when the copy is still running after this long, so a short copy does not flash it.
export const COPY_BAR_DELAY_MS = 300;

// A selection the engine reads: its ends, the pages it covers, and a way to hear that a covered
// page changed ('changed': the view's selection clears then, and so does the copy) or that the view
// closed ('closed': the copy stops quietly). Returns the unsubscribe.
export interface EnginePlan {
  readonly start: TextPos;
  readonly end: TextPos;
  readonly pages: number;
  readonly changes: (listener: (reason: 'changed' | 'closed') => void) => () => void;
}

export type LargeResult =
  | { readonly kind: 'text'; readonly parts: readonly string[] }
  | { readonly kind: 'tooLarge' | 'cancelled' | 'changed' };

// Reads the slices of a selection in order. Each slice's text is kept when it has any; the caller
// joins the kept parts with LINE_SEP, which is what TextSelection.text does across pages.
export async function runLargeCopy(
  rpc: Pick<PageRpc, 'chunked'>,
  handle: string,
  plan: EnginePlan,
  signal: AbortSignal,
  onProgress: (pagesDone: number, total: number) => void,
): Promise<LargeResult> {
  const parts: string[] = [];
  let length = 0;
  try {
    for await (const slice of rpc.chunked(
      'text.extract',
      { handle, start: plan.start, end: plan.end, budget_ms: COPY_BUDGET_MS },
      { signal },
    )) {
      if (signal.aborted) return { kind: 'cancelled' };
      if (slice.text !== '') {
        if (parts.length > 0) length += LINE_SEP.length;
        parts.push(slice.text);
        length += slice.text.length;
      }
      // Leaving the loop asks for no more slices, so the rest of the selection is never read.
      if (length > MAX_COPY_CHARS) return { kind: 'tooLarge' };
      onProgress(slice.pages_done, slice.total);
      if (slice.done) break;
    }
  } catch (error) {
    if (signal.aborted) return { kind: 'cancelled' };
    if (error instanceof RpcError && error.code === 'STALE_CURSOR') return { kind: 'changed' };
    throw error;
  }
  return { kind: 'text', parts };
}

// The text as a Blob: the parts with a line break between them, never joined into one string first.
export function textBlob(parts: readonly string[]): Blob {
  const pieces: string[] = [];
  parts.forEach((part, index) => {
    if (index > 0) pieces.push(LINE_SEP);
    pieces.push(part);
  });
  return new Blob(pieces, { type: 'text/plain' });
}

export function joinParts(parts: readonly string[]): string {
  return parts.join(LINE_SEP);
}

// What a finished copy can still write on the next press.
type Payload =
  | { readonly kind: 'blob'; readonly blob: Blob }
  | { readonly kind: 'parts'; readonly parts: readonly string[] };

// A copy whose text is here but was not written in the keypress. Its key is the selection's ends,
// so a press on another selection never writes it. Dropped when the covered pages change.
interface Prepared {
  readonly key: string;
  readonly payload: Payload;
  readonly unwatch: () => void;
}

export interface LargeCopyDeps {
  readonly rpc: Pick<PageRpc, 'chunked'>;
  readonly handle: string;
  readonly clipboard: Pick<Clipboard, 'writeText' | 'write'> | undefined;
  readonly store: ToastStore;
  readonly jobs?: JobProgressStore;
}

// One copy runs at a time: a second Copy while it runs is ignored (its bar is the one on screen).
let running = false;
let seq = 0;
let prepared: Prepared | null = null;

function keyOf(handle: string, plan: EnginePlan): string {
  return `${handle}|${plan.start.page}:${plan.start.index}|${plan.end.page}:${plan.end.index}`;
}

export function dropPrepared(): void {
  const old = prepared;
  prepared = null;
  old?.unwatch();
}

function keep(key: string, plan: EnginePlan, payload: Payload): void {
  dropPrepared();
  prepared = { key, payload, unwatch: plan.changes(dropPrepared) };
}

function isNotAllowed(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { name?: unknown }).name === 'NotAllowedError'
  );
}

function asRpcError(error: unknown): RpcError {
  if (error instanceof RpcError) return error;
  return new RpcError('INTERNAL', error instanceof Error ? error.message : String(error));
}

// Writes a payload now. It is called inside a keypress or a click, so the browser still takes it.
function writePayload(
  clipboard: Pick<Clipboard, 'writeText' | 'write'>,
  payload: Payload,
  store: ToastStore,
): void {
  const written: Promise<void> =
    payload.kind === 'blob'
      ? clipboard.write([new ClipboardItem({ 'text/plain': payload.blob })])
      : clipboard.writeText(joinParts(payload.parts));
  written.then(
    () => store.push('success', t('copy.done')),
    () => store.push('error', t('copy.failed')),
  );
}

// The toast for a copy that wrote nothing. A cancel the user asked for shows nothing.
function reportStopped(
  store: ToastStore,
  res: LargeResult,
  changed: boolean,
  refused: boolean,
): void {
  if (changed || res.kind === 'changed') store.push('info', t('copy.changed'));
  else if (refused) store.push('info', t('copy.busy'));
  else if (res.kind === 'tooLarge') store.push('info', t('copy.tooLarge'));
}

// Copies a large selection. It runs up to the clipboard call synchronously, so the keypress still
// counts as the user's. Returns at once; the outcome shows on the bar and in toasts.
export function copyLarge(deps: LargeCopyDeps, plan: EnginePlan): void {
  const { store } = deps;
  const key = keyOf(deps.handle, plan);
  const clipboard = deps.clipboard;
  // The second press of the fallback: the text is already here, so it is written now.
  if (prepared !== null && prepared.key === key && clipboard !== undefined) {
    const { payload } = prepared;
    dropPrepared();
    writePayload(clipboard, payload, store);
    return;
  }
  dropPrepared();
  if (running) return;
  if (clipboard === undefined) {
    store.push('error', t('copy.failed'));
    return;
  }
  const jobs = deps.jobs ?? jobProgress;
  if (jobs.getState().job !== undefined) {
    store.push('info', t('copy.busy'));
    return;
  }

  running = true;
  const controller = new AbortController();
  const token = `copy-${++seq}`;
  let changed = false;
  let refused = false;
  let shown = false;
  const unwatch = plan.changes((reason) => {
    if (reason === 'changed') changed = true;
    controller.abort();
  });
  // The bar comes after COPY_BAR_DELAY_MS. If another job's bar is up by then, this copy is refused.
  const timer = setTimeout(() => {
    shown = jobs.begin({ token, label: t('copy.progress'), terminate: () => controller.abort() });
    if (!shown) {
      refused = true;
      controller.abort();
    }
  }, COPY_BAR_DELAY_MS);
  const settle = (): void => {
    clearTimeout(timer);
    if (shown) jobs.finish(token, { ok: true, quiet: true });
    unwatch();
    running = false;
  };
  const readyAction =
    (key: string): (() => void) =>
    () => {
      const ready = prepared;
      if (ready === null || ready.key !== key) return;
      dropPrepared();
      writePayload(clipboard, ready.payload, store);
    };

  const result = runLargeCopy(deps.rpc, deps.handle, plan, controller.signal, (done, total) => {
    if (shown && total > 0) jobs.progress(token, Math.round((done * 100) / total));
  });

  // No write in the keypress: the text waits for the next press (the toast's button, or Copy). It
  // is also the fallback when the browser throws at once. settle() runs only when the read ends:
  // until then the slices are still being read, so the bar, the watcher and `running` must stay.
  const keepForNextPress = (): void => {
    result.then(
      (res) => {
        settle();
        if (changed || res.kind !== 'text') return reportStopped(store, res, changed, refused);
        if (res.parts.length === 0) return store.push('info', t('copy.nothingSelected'));
        keep(key, plan, { kind: 'parts', parts: res.parts });
        store.push('info', t('copy.ready'), {
          label: t('copy.readyAction'),
          run: readyAction(key),
        });
      },
      (error: unknown) => {
        settle();
        reportRpcError(store, asRpcError(error));
      },
    );
  };

  if (typeof ClipboardItem === 'undefined' || typeof clipboard.write !== 'function') {
    keepForNextPress();
    return;
  }

  // The write starts now, with the data as a promise: the browser takes the keypress for it. A
  // rejected data promise (no text, or a stopped copy) makes the write fail, so nothing is written.
  const blob = result.then((res) => {
    if (res.kind !== 'text' || res.parts.length === 0) throw res;
    return textBlob(res.parts);
  });
  // Handled here too, so a fallback that never reads the blob does not leave a rejection unhandled.
  blob.catch(() => undefined);
  let written: Promise<{ ok: true } | { ok: false; error: unknown }>;
  try {
    written = clipboard.write([new ClipboardItem({ 'text/plain': blob })]).then(
      () => ({ ok: true as const }),
      (error: unknown) => ({ ok: false as const, error }),
    );
  } catch {
    // The browser threw instead of refusing: nothing was written, and the text waits as above.
    keepForNextPress();
    return;
  }
  void Promise.all([result, written, blob.catch(() => null)]).then(
    ([res, write, data]) => {
      settle();
      // A change that landed after the last slice, but before this handler, still makes the text
      // stale: it is reported as changed, and no payload is kept for a later press.
      if (changed || res.kind !== 'text') return reportStopped(store, res, changed, refused);
      if (res.parts.length === 0) return store.push('info', t('copy.nothingSelected'));
      if (write.ok) return store.push('success', t('copy.done'));
      if (isNotAllowed(write.error) && data !== null) {
        // Refused outside the keypress: the Blob waits for the next press.
        keep(key, plan, { kind: 'blob', blob: data });
        store.push('info', t('copy.ready'), {
          label: t('copy.readyAction'),
          run: readyAction(key),
        });
        return;
      }
      store.push('error', t('copy.failed'));
    },
    (error: unknown) => {
      settle();
      reportRpcError(store, asRpcError(error));
    },
  );
}
