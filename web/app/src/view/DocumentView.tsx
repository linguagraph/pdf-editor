// The document view (W5.1a, W1.4 sections 1, 2, 5, 6 and 8): the pages of one open document,
// stacked vertically at the zoom level, each with a placeholder at its size. Only the pages near the
// viewport are mounted; the visible ones are rendered through render.page, queued and cached by the
// render scheduler, which the thumbnails panel of the same tab shares. Each page also gets its text
// layer (W5.4) and the search highlights. Pages above 4 MP are drawn as tiles (W5.1b, tiles.ts);
// comments are drawn into the page image by the engine.
import {
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type Ref,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { PageRpc } from '../engine/engineConnection';
import { t, tp } from '../i18n/t';
import {
  onPreferencesChanged,
  readPreferences,
  writePreferences,
  type PageLayoutChoice,
  type ZoomChoice,
} from '../prefs/store';
import {
  computeColumn,
  computeSpreads,
  currentPageIndex,
  fitPageZoom,
  fitWidthZoom,
  largestPage,
  type FitMode,
  type PageBox,
  type PageSizePt,
  scrollTopForPage,
  scrollForRect,
  scrollTopForAnchor,
  type PageRectPt,
  type ViewAnchor,
  singlePageScene,
  type ViewLayout,
  viewAnchor,
  visibleRange,
} from './layout';
import { PageMarks } from '../search/PageMarks';
import { marksOnPage, type SearchMarks } from '../search/search';
import {
  type CopyPlan,
  planCopy,
  readModelIndexes,
  selectPageText as selectPageTextLayer,
} from '../text/copy';
import { toasts } from '../feedback/toasts';
import { MODEL_OFF, type ModelRead, modelSelectionAreas } from '../text/selection';
import type { PageArea } from '../text/textLayer';
import { TextSelection } from '../text/textSelection';
import { PageTextLayer } from '../text/PageTextLayer';
import { useTextSelection } from '../text/useTextSelection';
import { linksActive, textLayerPassive } from '../text/textLayerPassive';
import { LinkCache } from '../links/linkCache';
import { PageLinkLayer } from '../links/PageLinkLayer';
import { LinkConfirmDialog } from '../links/LinkConfirmDialog';
import { classifyLink, type LinkConfirm, type LinkNotice } from '../links/linkTarget';
import { isTap, type LinkPress } from '../links/linkTap';
import type { LinkDto } from '../rpc/generated';
import { useHandPan } from './useHandPan';
import { isShapeTool, type ViewTool } from './viewTool';
import { CropLayer } from '../crop/CropLayer';
import { marginsFromBox, type DrawnCrop } from '../crop/cropChoice';
import { CommentLayer } from '../comments/CommentLayer';
import { CommentToolbar } from '../comments/CommentToolbar';
import type { PageRect } from '../comments/coordinates';
import { placeComment } from '../comments/placement';
import type { CommentToolId } from '../comments/tools';
import type { ActionJobWorker } from '../actionJob/actionJob';
import type { RedactionController } from '../redaction/controller';
import { RedactLayer, SelectedMarkOutlines } from '../redaction/RedactLayer';
import { RedactionBar } from '../redaction/RedactionBar';
import { selectedRectsOn } from '../redaction/controller';
import { useRedaction } from '../redaction/useRedaction';
import type { TextLine } from '../text/textLayer';
import { MAX_TEXT_LAYERS, TextLayerCache } from '../text/textLayerCache';
import { commitEdit, type EditChange } from '../edit/commit';
import { EditLayer, type EditSelection } from '../edit/EditLayer';
import { ObjectCache } from '../edit/objectCache';
import { ShapeLayer } from '../edit/ShapeLayer';
import { addShape, noteShapeTooSmall, type ShapeSpec, SHAPE_KIND } from '../edit/shapes';
import { CachedCanvas } from './pixels';
import { useDevicePixelRatio } from './useDevicePixelRatio';
import { RenderScheduler } from './renderScheduler';
import { planPageRenders, tileRect, tilesInViewport } from './tiles';
import { CanvasPill } from './CanvasPill';
import { CSS_PX_PER_POINT, DEFAULT_ZOOM, clampZoom, scaleMilli, stepZoom } from './zoom';
import type { ZoomRequest } from './zoomBox';
import {
  anchoredScroll,
  isZoomWheel,
  keyScrollPixels,
  takeWheelSteps,
  takeWholePixels,
  wheelDeltaPixels,
} from './wheel';
import {
  isScaledSpace,
  sceneTopForScroll,
  scrollPerScenePixel,
  scrollTopForSceneTop,
  spacerHeight,
} from './scrollSpace';
import './documentView.css';

export { MAX_RENDER_ATTEMPTS } from './renderScheduler';

// A change the view reports to the copies it has started: pages moved (null: the page count did), or
// the view closed.
type CopyEvent =
  | { readonly kind: 'changed'; readonly pages: readonly number[] | null }
  | { readonly kind: 'closed' };

function notifyCopies(watchers: Set<(event: CopyEvent) => void>, event: CopyEvent): void {
  for (const watcher of [...watchers]) watcher(event);
}

// Whether one of the pages lies in from..to, both included.
function anyInRange(pages: readonly number[], from: number, to: number): boolean {
  for (const page of pages) {
    if (from <= page && page <= to) return true;
  }
  return false;
}

// Hears the changes that touch the pages from..to (a copy's range), or any change to the count.
function watchRange(
  watchers: Set<(event: CopyEvent) => void>,
  from: number,
  to: number,
  listener: (reason: 'changed' | 'closed') => void,
): () => void {
  const watcher = (event: CopyEvent): void => {
    if (event.kind === 'closed') listener('closed');
    else if (event.pages === null || anyInRange(event.pages, from, to)) listener('changed');
  };
  watchers.add(watcher);
  return () => {
    watchers.delete(watcher);
  };
}

// The page layouts the toolbar offers: the continuous single column, or two-page spreads.

// What the parent can ask the view to do: the ribbon's View and Home commands and the shortcuts reach
// the view through here (W4.2a S6), as the canvas pill does from inside.
export interface DocumentViewHandle {
  // Scrolls so the page's top edge is at the top of the viewport. Out-of-range indexes do nothing.
  // A link's target passes its height in points (top), which lands that point at the top instead.
  scrollToPage(index: number, top?: number): void;
  // A zoom request (a fit mode or a percentage), or one step of the zoom in either direction.
  zoom(request: ZoomRequest | { readonly step: -1 | 1 }): void;
  // Moves to the next or the previous page, clamped to the document.
  stepPage(direction: -1 | 1): void;
  // Focuses the canvas pill's page field and selects its text (the status bar's Go to Page, W4.3b).
  focusPageField(): void;
  // Deletes the objects the Edit tool has selected (the Delete command and key, W6.7a S5). Nothing
  // happens with no selection.
  deleteEditSelection(): void;
  // Sets the page layout (single column or two-page spreads, the cover alone or not), keeping the
  // page at the top in view (the ribbon's View commands, W4.3a S6).
  setLayout(choice: { readonly layout: ViewLayout; readonly firstAlone: boolean }): void;
  // Turns night reading on or off and saves the choice, as the toolbar's button does.
  setNight(on: boolean): void;
  // The text lines of a page as they are shown, or undefined until they arrive (W4.12: the selection maps them).
  cachedLines(page: number): readonly TextLine[] | undefined;
  // Turns a comment tool on, or off with null (W4.3a S7). A tool turned on sets the view tool to Select.
  setCommentTool(tool: CommentToolId | null): void;
  // Turns the redaction mark tool on or off, as the toolbar's Mark button does (W4.3a S7).
  setRedacting(on: boolean): void;
  // Selects the text of the whole document, for Select All (W5.4 section 2): the model's selection
  // from the first caret to the last page's end, once the last page's length has arrived; it says
  // so with a toast. Under Hand, a DOM range over the page at the top, which the browser holds.
  selectAll(): void;
  // How Copy reads the model's selection (W5.4 section 3.1). `off` when the model is not in charge
  // (the Hand tool), so the browser's selection is copied instead.
  copyPlan(): CopyPlan;
  // The number of pages the model's selection covers (0 when none, or under Hand). Mark Text
  // refuses more than MAX_TEXT_LAYERS (W5.4 section 6).
  selectedPageSpan(): number;
  // The areas of the model's selection, one per page (Mark Text). `off` as selectedText.
  selectedAreas(): ModelRead<PageArea[]>;
  // Clears the model's selection (Mark Text does so once it has marked the selection).
  clearTextSelection(): void;
  // Asks for the text indexes of these pages and resolves once they have been asked for (Mark Text
  // waits on them; Copy only asks). At most MAX_TEXT_LAYERS pages are asked for.
  loadTextPages(pages: readonly number[]): Promise<void>;
  // Scrolls so a rectangle on the page (in points) is in view, as the search panel does for a hit.
  revealRect(index: number, rect: PageRectPt): void;
}

// What the view reports to its host for the status bar and the ribbon's checked states: the page at
// the middle of the viewport (one-based), the zoom, and the document's page labels (one per page;
// empty when it has none), plus the page count, the fit mode and the view's layout and night flags.
export interface DocumentViewState {
  readonly page: number;
  readonly zoom: number;
  // The page layout the reader chose (single column or two-page spreads).
  readonly layout: ViewLayout;
  readonly labels: readonly string[];
  readonly pageCount: number;
  // The fit mode in effect, or null for a manual zoom.
  readonly fit: FitMode | null;
  readonly firstAlone: boolean;
  readonly night: boolean;
}

export interface DocumentViewProps {
  readonly rpc: PageRpc;
  readonly handle: string;
  // The tab's scheduler, shared with the thumbnails panel. When absent the view makes its own.
  readonly scheduler?: RenderScheduler;
  readonly ref?: Ref<DocumentViewHandle>;
  // Called when the page or the zoom changes, and once when the view mounts. Give it a stable
  // function (a state setter, for one) so the view does not report again on each render.
  readonly onViewChange?: (state: DocumentViewState) => void;
  // The search hits to draw on the pages, and the selected one (W5.4).
  readonly searchMarks?: SearchMarks;
  // Starts the job worker for the redaction apply (W7.4). Tests leave it out; applying then fails.
  readonly startWorker?: () => ActionJobWorker;
  // The document's redaction controller, made by the app per session (W7.4b) so the toolbar and the
  // redactions panel share it. The view makes its own when absent.
  readonly redaction?: RedactionController;
  // The view tool (W4.10): Hand pans with a drag. The app keeps it per tab; Select when absent.
  readonly tool?: ViewTool;
  // The tab this view shows (W6.7b). A shape add that settles later hands it back with the reset
  // to Select, so the reset lands on the tab that drew the shape, not on the tab now shown.
  readonly tabId?: number | null;
  // Called when the view itself asks for another tool: a comment or mark tool chosen here sets it
  // back to Select, so the two never read as on together. A shape add passes the tab it drew on.
  readonly onToolChange?: (tool: ViewTool, tabId?: number | null) => void;
  // The crop box tool (W4.11 S5) reports the box it drew, as margins on its page (1-based), or null
  // when Esc ends the tool with no box. The app ends the tool and reopens the crop dialog.
  readonly onCropBoxEnd?: (drawn: DrawnCrop | null) => void;
  // Reports which comment tool is on (or null), so the ribbon shows it pressed (W4.3a S7). Called
  // on mount too, so a remounted view reports its reset state.
  readonly onCommentToolChange?: (tool: CommentToolId | null) => void;
  // A link jumped to a page of this document (W5.3). The app records the jump in the tab's history
  // and scrolls; without it the view scrolls itself and records nothing (tests).
  readonly onLinkGoTo?: (page: number, top: number | null) => void;
}

// How long a notice from a link that did nothing stays in the view (W5.3).
const LINK_NOTICE_MS = 8000;

const LINK_NOTICE_KEYS = {
  noTarget: 'links.notice.noTarget',
  unsupported: 'links.notice.unsupported',
  malformed: 'links.notice.malformed',
} as const;

// The text of a notice from a link. A blocked link names its scheme, as plain text.
function linkNoticeText(notice: LinkNoticeState): string {
  if (notice.notice === 'blocked')
    return t('links.notice.blocked', { scheme: notice.scheme ?? '' });
  return t(LINK_NOTICE_KEYS[notice.notice]);
}

interface LinkNoticeState {
  readonly notice: LinkNotice;
  readonly scheme?: string;
}

// The link under a press, with its page and its index in that page's sorted list (W5.3). Null when
// the press is not a primary press on a link.
function linkPressAt(event: ReactPointerEvent<HTMLElement>): LinkPress | null {
  if (event.button !== 0 || !(event.target instanceof Element)) return null;
  const link = event.target.closest<HTMLElement>('.page-link');
  const pageElement = link?.closest<HTMLElement>('.doc-page') ?? null;
  if (link === null || pageElement === null) return null;
  return {
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    page: Number(pageElement.dataset.page),
    index: Number(link.dataset.link),
  };
}

// The scroll offset, in CSS pixels, of a point a link names on its page: its height in points,
// kept on the page. No point gives no offset.
function targetOffsetPx(topPt: number | undefined, heightPt: number, zoom: number): number {
  if (topPt === undefined) return 0;
  return Math.min(heightPt, Math.max(0, topPt)) * zoom * CSS_PX_PER_POINT;
}

// A button, link or ARIA widget inside the scroller keeps its own Space and arrow keys (W5.1a S4 fix).
// The match must lie inside the scroller: an ancestor outside it (a region around the whole view)
// does not make the scroller's keys belong to a control. role="group" is not a control: each page
// div has it, and a key on a page's own content must still scroll the scene.
const INTERACTIVE_SELECTOR = 'button, a[href], [role]:not([role="group"])';

function isInsideControl(target: HTMLElement, scroller: HTMLElement): boolean {
  const control = target.closest<HTMLElement>(INTERACTIVE_SELECTOR);
  return control !== null && control !== scroller && scroller.contains(control);
}

// The job worker is missing: the apply reports its failure, as any other failed job does.
function noJobWorker(): ActionJobWorker {
  throw new Error('the job worker is not available');
}

// The viewport size, or the window's when the element has no layout yet (jsdom, first paint). The
// top is a scene position (scrollSpace.ts), so it is the layout's coordinate whatever the scroll space.
function viewportOf(
  element: HTMLElement | null,
  contentHeight: number,
): { top: number; height: number; width: number } {
  const height = element?.clientHeight || window.innerHeight;
  return {
    top: sceneTopForScroll(element?.scrollTop ?? 0, contentHeight, height),
    height,
    width: element?.clientWidth || window.innerWidth,
  };
}

// Sets an element's scroll position from a scene position (the inverse of viewportOf's top).
function scrollToScene(element: HTMLElement, sceneTop: number, contentHeight: number): void {
  element.scrollTop = scrollTopForSceneTop(
    sceneTop,
    contentHeight,
    element.clientHeight || window.innerHeight,
  );
}

// The fit mode a saved default zoom asks for; Actual size is no fit (the zoom is 100 %).
function fitForChoice(choice: ZoomChoice): FitMode | null {
  if (choice === 'fitWidth') return 'width';
  if (choice === 'fitPage') return 'page';
  return null;
}

// The saved default page layout as the view's two settings. Two pages with the first page alone is
// the cover layout (W4.5 S2); the single-page layout shows one page at a time (W5.1a S3).
function layoutForChoice(choice: PageLayoutChoice): { layout: ViewLayout; firstAlone: boolean } {
  if (choice === 'single') return { layout: 'single', firstAlone: false };
  if (choice === 'twoPage') return { layout: 'twoPage', firstAlone: false };
  if (choice === 'twoPageCover') return { layout: 'twoPage', firstAlone: true };
  return { layout: 'continuous', firstAlone: false };
}

export function DocumentView({
  rpc,
  handle,
  scheduler: shared,
  ref,
  onViewChange,
  searchMarks,
  startWorker,
  redaction,
  tool = 'select',
  tabId = null,
  onToolChange,
  onCropBoxEnd,
  onCommentToolChange,
  onLinkGoTo,
}: DocumentViewProps) {
  const [sizes, setSizes] = useState<readonly PageSizePt[] | null>(null);
  // The page labels, from the same session.pages reply as the sizes (W5.3).
  const [labels, setLabels] = useState<readonly string[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  // The zoom the reader picked by steps, the wheel or the presets; used when no fit mode is on.
  const [manualZoom, setManualZoom] = useState(DEFAULT_ZOOM);
  // The fit mode, if any. The saved default zoom starts it when the document opens (W5.1e).
  const [fit, setFit] = useState<FitMode | null>(() => fitForChoice(readPreferences().defaultZoom));
  // The default the view last applied, so a save that leaves it unchanged does not reset the zoom.
  const appliedChoice = useRef<ZoomChoice>(readPreferences().defaultZoom);
  // The default page layout (W4.5 S2) starts the view, and a changed default applies the same way
  // the zoom default does; the reader's own choice is kept until the default changes.
  const [startLayout] = useState<PageLayoutChoice>(() => readPreferences().pageLayout);
  const appliedLayout = useRef<PageLayoutChoice>(startLayout);
  const [viewLayout, setViewLayout] = useState<ViewLayout>(
    () => layoutForChoice(startLayout).layout,
  );
  const [firstAlone, setFirstAlone] = useState(() => layoutForChoice(startLayout).firstAlone);
  // The page the single-page layout shows. It is not the scroll: that layout's scroll stays in the
  // page, so only a jump, a step or a change of layout moves it.
  const [singlePage, setSinglePage] = useState(0);
  // Night reading is a saved preference: read once here, written when the toggle changes.
  const [night, setNight] = useState(() => readPreferences().nightReading);
  // The zoom as the latest wheel event left it, so a burst of events builds on each other.
  const zoomRef = useRef(manualZoom);
  // The canvas pill's page field, which focusPageField reaches; set while the pill is mounted.
  const pageFieldRef = useRef<HTMLInputElement>(null);
  // A shape add in flight (W6.7b): a second gesture or Enter is ignored until it settles.
  const shapeInFlight = useRef(false);
  const wheelPending = useRef(0);
  // The fraction of a pixel the scaled scroll still owes the spacer (one scroller per view), carried
  // from one wheel or key event to the next so small deltas are not lost (W5.1a S4 fix).
  const scaledRemainder = useRef(0);
  // Where the content point under the pointer must be scrolled to once the new zoom is laid out.
  const pendingAnchor = useRef<{ left: number; top: number } | null>(null);
  // The page at the top of the view, kept up to date (the selection of the page at the top uses it).
  const topPage = useRef(0);
  // Where the reader is (page and offset within it), so a zoom, a fit refit or a layout change keeps
  // the place (W5.1a S2). It is read at each measure, against the boxes on screen then, and the
  // handlers that change the zoom or the layout read it again before they change anything.
  const readingPlace = useRef<ViewAnchor | null>(null);
  // The boxes of the layout on screen, as last committed; a measure reads them, not a stale closure.
  const boxesOnScreen = useRef<readonly PageBox[]>([]);
  // The full height of the layout on screen (the scene). Above the scroll-space limit the scroll
  // position maps onto it (scrollSpace.ts); the reads and writes of scrollTop go through that map.
  const contentHeight = useRef(0);
  // The pointer drag ratio of the Hand tool needs the layout, so the hook is called after it.
  // The page holding keyboard focus, kept mounted whatever the scroll position (W5.1a risks).
  const [focusedPage, setFocusedPage] = useState<number | null>(null);
  const [scroll, setScroll] = useState({ top: 0, height: 0, width: 0 });
  // Bumped whenever the scheduler finished a render; the queue is re-planned then.
  const [tick, setTick] = useState(0);
  const [ownScheduler] = useState(() => new RenderScheduler(rpc, handle));
  const scheduler = shared ?? ownScheduler;
  const scrollRef = useRef<HTMLDivElement>(null);
  // The comment tool on, if any (W7.1a): a click or drag on a page then places a comment.
  const [commentTool, setCommentTool] = useState<CommentToolId | null>(null);
  // The redaction tools (W7.4). The mark tool and a comment tool are not on together.
  const redactionTools = useRedaction({
    rpc,
    handle,
    scheduler,
    startWorker: startWorker ?? noJobWorker,
    controller: redaction,
  });
  // The toolbar and the ribbon choose tools through these, so both do the same thing. A comment
  // tool or the mark tool is never on with the other, and turning one on sets the view tool to Select.
  const chooseCommentTool = useCallback(
    (chosen: CommentToolId | null) => {
      redactionTools.controller.setActive(false);
      setCommentTool(chosen);
      if (chosen !== null) onToolChange?.('select');
    },
    [redactionTools.controller, onToolChange],
  );
  const chooseRedacting = useCallback(
    (on: boolean) => {
      redactionTools.controller.setActive(on);
      if (on) {
        setCommentTool(null);
        onToolChange?.('select');
      }
    },
    [redactionTools.controller, onToolChange],
  );
  useEffect(() => {
    onCommentToolChange?.(commentTool);
  }, [commentTool, onCommentToolChange]);
  // The Hand and crop box tools and the comment or mark tools are not on together: choosing one of
  // the view tools turns them off (the last tool chosen wins, as the desktop has one tool per
  // document). The comment tool is reset while rendering, when the tool prop changes, so no effect
  // sets state.
  // The Edit tool's object lists (W6.7a S4), one cache per document, and the object selection, which
  // is dropped when the tool is left (its keys mean nothing to another tool).
  const editObjects = useMemo(() => new ObjectCache(rpc, handle), [rpc, handle]);
  const [editSelection, setEditSelection] = useState<EditSelection | null>(null);
  const [seenTool, setSeenTool] = useState(tool);
  if (seenTool !== tool) {
    setSeenTool(tool);
    if (tool !== 'select') setCommentTool(null);
    if (tool !== 'edit') setEditSelection(null);
  }
  // A gesture the layer ended with a drag is committed here; the engine makes the change and the
  // selection it leaves is set by commitEdit (W6.7a S5).
  const commitChange = useCallback(
    (change: EditChange) =>
      void commitEdit({ rpc, handle, cache: editObjects, scheduler }, change, setEditSelection),
    [rpc, handle, editObjects, scheduler],
  );
  // The selected objects of the page are deleted as one undo step. The keys are read from the list
  // the layer shows, so nothing is asked of the engine first; with no selection, nothing is sent.
  const deleteEditSelection = useCallback(() => {
    const sel = editSelection;
    if (sel === null || sel.keys.length === 0) return;
    if (sel.revision !== scheduler.revisionOf(sel.page)) return;
    const list = editObjects.ready(sel.page, sel.revision);
    if (list === undefined) return;
    const objects = list.objects.filter((o) => sel.keys.includes(o.key));
    commitChange({ kind: 'delete', page: sel.page, revision: sel.revision, objects });
  }, [editSelection, editObjects, scheduler, commitChange]);
  // Delete and Backspace delete the Edit tool's selection, unless a field or a dialog has the key.
  useEffect(() => {
    if (tool !== 'edit') return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Delete' && event.key !== 'Backspace') return;
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      if (document.querySelector('.overlay-backdrop')) return;
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        target.closest('input, textarea, select, [contenteditable]')
      )
        return;
      event.preventDefault();
      deleteEditSelection();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [tool, deleteEditSelection]);
  useEffect(() => {
    if (tool === 'select') return;
    redactionTools.controller.setActive(false);
  }, [tool, redactionTools.controller]);
  // Esc ends the crop box tool with no box, from anywhere in the page (the pointer may have left the
  // view's focus), and the dialog reopens unchanged. An Esc another part already handled, or one
  // meant for an open dialog (which owns Escape, as in useDismissablePanels), is left alone.
  useEffect(() => {
    if (tool !== 'cropBox') return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      if (document.querySelector('.overlay-backdrop')) return;
      onCropBoxEnd?.(null);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [tool, onCropBoxEnd]);
  // The text layers of this document, one per page and revision (W5.4).
  const textLayers = useMemo(() => new TextLayerCache(rpc, handle), [rpc, handle]);
  // The links of each page (W5.3), one cache per document, like the text layers. A press on a link
  // is kept from its pointer-down until the release decides whether it was a tap.
  const linkLists = useMemo(() => new LinkCache(rpc, handle), [rpc, handle]);
  const [linkNotice, setLinkNotice] = useState<LinkNoticeState | null>(null);
  // The web or mail link waiting for the reader's confirmation (W5.3); null when no dialog is open.
  const [linkConfirm, setLinkConfirm] = useState<LinkConfirm | null>(null);
  const linkPress = useRef<LinkPress | null>(null);
  useEffect(() => {
    if (linkNotice === null) return;
    const id = window.setTimeout(() => setLinkNotice(null), LINK_NOTICE_MS);
    return () => window.clearTimeout(id);
  }, [linkNotice]);

  useEffect(() => {
    let active = true;
    rpc
      .call('session.pages', { handle })
      .then((reply) => {
        if (active) {
          setSizes(reply.pages);
          setLabels(reply.labels);
        }
      })
      .catch(() => {
        if (active) setLoadFailed(true);
      });
    return () => {
      active = false;
    };
  }, [rpc, handle]);

  useEffect(() => scheduler.subscribe(() => setTick((value) => value + 1)), [scheduler]);

  // The zoom in effect: the fitted one while a fit mode is on (it follows the viewport size, so a
  // resize refits), else the manual zoom. The fit is the widest and tallest page, so every page fits.
  const columns = viewLayout === 'twoPage' ? 2 : 1;
  const fitted =
    fit === null
      ? null
      : fit === 'width'
        ? fitWidthZoom(largestPage(sizes ?? []).width, scroll.width, columns)
        : fitPageZoom(
            largestPage(sizes ?? []).width,
            largestPage(sizes ?? []).height,
            scroll.width,
            scroll.height,
            columns,
          );
  const zoom = fitted ?? manualZoom;

  // Records where the reader is, from the scroll position on screen and the boxes laid out there.
  // Called before any change of zoom or layout, so the place is the one the reader saw. Stable: it
  // reads refs only, so the handlers that use it can keep empty dependency lists.
  const capturePlace = useCallback(() => {
    const element = scrollRef.current;
    if (element === null) return;
    readingPlace.current = viewAnchor(
      boxesOnScreen.current,
      viewportOf(element, contentHeight.current).top,
    );
  }, []);

  // Changes the page layout and keeps the place: the saved default, the toolbar and the handle all
  // come through here, so a layout change never loses the page the reader was on (W4.5 S2).
  const changeLayout = useCallback(
    (next: { readonly layout: ViewLayout; readonly firstAlone: boolean }) => {
      capturePlace();
      if (next.layout === 'single') setSinglePage(readingPlace.current?.page ?? 0);
      setViewLayout(next.layout);
      setFirstAlone(next.firstAlone);
    },
    [capturePlace],
  );

  // Leaves any fit mode for a manual zoom: the steps, the wheel and the presets all do this.
  const zoomTo = (value: number) => {
    capturePlace();
    setFit(null);
    setManualZoom(value);
  };

  const layout = useMemo(
    () =>
      viewLayout === 'single'
        ? singlePageScene(sizes ?? [], zoom, singlePage)
        : viewLayout === 'twoPage'
          ? computeSpreads(sizes ?? [], zoom, firstAlone)
          : computeColumn(sizes ?? [], zoom),
    [sizes, zoom, viewLayout, firstAlone, singlePage],
  );
  // The Hand tool's drag moves the scene one pixel per pixel dragged, so it scrolls by this ratio
  // above the scaled-space limit (W5.1a S4).
  // The Select tool: a press on text selects it (W5.4 S3) and a press off text pans, as on the
  // desktop. An armed gesture tool takes the drag instead (textLayerPassive), so neither happens.
  const selecting =
    tool === 'select' &&
    !textLayerPassive({
      commentTool,
      viewTool: tool,
      markToolOn: redactionTools.state.active,
    });
  const textSelection = useTextSelection({
    lookup: textLayers,
    revisionOf: (page) => scheduler.revisionOf(page),
    zoom,
    scroll: scrollRef,
    enabled: selecting,
  });
  const selectModel = textSelection.select;
  // The model's selection as of the last render, read by Copy (copyPlan) outside the render. It
  // counts only while the Select tool is on (`selecting`): once Hand or an armed comment or redaction
  // tool takes over, a stale selection must not override the browser's native one.
  const modelSelection = useRef<TextSelection | null>(null);
  const selectingNow = useRef(false);
  // Select All while the last page's length is loading (W5.4 section 2.3). Any change or press drops it.
  const selectAllPending = useRef<{ readonly page: number; readonly revision: number } | null>(
    null,
  );
  // The copies that read a range of this view (W5.4 section 3.3). Each one hears a change to a page
  // in its range, or a page-count change (pages null), and stops; a closed view stops them quietly.
  const copyWatchers = useRef(new Set<(event: CopyEvent) => void>());
  useEffect(() => {
    selectingNow.current = selecting;
    modelSelection.current = selecting ? textSelection.selection : null;
  });
  // A change to a covered page (Undo, Redo, an edit) makes the selection's offsets stale, so it is dropped.
  useEffect(() => {
    return scheduler.subscribeRevisions((pages) => {
      const sel = modelSelection.current;
      const covered =
        sel !== null && !sel.isEmpty && anyInRange(pages, sel.start.page, sel.end.page);
      // A change to any page makes a pending Select All stale (its length may be of the old text).
      selectAllPending.current = null;
      if (covered) {
        modelSelection.current = null;
        selectModel(null);
      }
      notifyCopies(copyWatchers.current, { kind: 'changed', pages });
    });
  }, [scheduler, selectModel]);
  // A closed view (its tab shut or the document replaced) stops its copies quietly.
  useEffect(() => {
    const watchers = copyWatchers.current;
    return () => notifyCopies(watchers, { kind: 'closed' });
  }, []);
  // Leaving Select clears the model's selection, so it is neither drawn nor copied later.
  useEffect(() => {
    if (!selecting) {
      selectAllPending.current = null;
      selectModel(null);
    }
  }, [selecting, selectModel]);
  const pan = useHandPan(
    tool === 'hand' || selecting,
    scrollPerScenePixel(layout.height, scroll.height || window.innerHeight),
  );

  // Reads the scroll position and the viewport size into state: on scroll, on resize (the window's,
  // and the element's own, which moves when the layout around it does) and once the pages are known.
  // A width or height change under 1 px is ignored: a scrollbar appearing or a rounding step must not
  // make the fitted zoom flip back and forth between two values.
  const measure = useCallback(() => {
    // Read before the new size is set: the refit that follows must start from this place.
    capturePlace();
    const next = viewportOf(scrollRef.current, contentHeight.current);
    setScroll((prev) => ({
      top: next.top,
      height: Math.abs(next.height - prev.height) >= 1 ? next.height : prev.height,
      width: Math.abs(next.width - prev.width) >= 1 ? next.width : prev.width,
    }));
  }, [capturePlace]);
  useEffect(() => {
    measure();
    window.addEventListener('resize', measure);
    const observer =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => measure());
    if (observer !== null && scrollRef.current !== null) observer.observe(scrollRef.current);
    return () => {
      window.removeEventListener('resize', measure);
      observer?.disconnect();
    };
  }, [sizes, measure]);

  // The saved default zoom, applied when the user saves a different one while the document is open.
  // Saving other preferences leaves the zoom alone.
  useEffect(
    () =>
      onPreferencesChanged(() => {
        const choice = readPreferences().defaultZoom;
        if (choice === appliedChoice.current) return;
        appliedChoice.current = choice;
        setFit(fitForChoice(choice));
        if (choice === 'actual') setManualZoom(DEFAULT_ZOOM);
      }),
    [],
  );

  // The saved default page layout, applied the same way: only a changed choice moves the view.
  useEffect(
    () =>
      onPreferencesChanged(() => {
        const choice = readPreferences().pageLayout;
        if (choice === appliedLayout.current) return;
        appliedLayout.current = choice;
        const next = layoutForChoice(choice);
        changeLayout(next);
      }),
    [changeLayout],
  );

  // Brings a rectangle of a page into view. Out-of-range indexes do nothing.
  const revealRect = useCallback(
    (index: number, rect: PageRectPt) => {
      const element = scrollRef.current;
      const box = layout.boxes.find((candidate) => candidate.index === index);
      if (element === null || box === undefined) return;
      const target = scrollForRect(
        box,
        rect,
        zoom,
        { top: element.scrollTop, left: element.scrollLeft },
        {
          height: element.clientHeight || window.innerHeight,
          width: element.clientWidth || window.innerWidth,
        },
      );
      // The target is a scene position: the scroll position comes from the scaled space's map.
      scrollToScene(element, target.top, layout.height);
      element.scrollLeft = target.left;
      capturePlace();
      setScroll(viewportOf(element, layout.height));
    },
    [layout, zoom, capturePlace],
  );

  // Scrolls so the page's top edge is at the top of the viewport; shared by the handle and the pill.
  const scrollToPage = useCallback(
    (index: number, topPt?: number) => {
      const element = scrollRef.current;
      // A link's target lands below the page's top by its height in points (W5.3); no target lands
      // at the top, as before.
      const offset = targetOffsetPx(topPt, sizes?.[index]?.height ?? 0, zoom);
      if (viewLayout === 'single') {
        // One page is shown, so the jump changes the page and lands at that page's top. The place is
        // set to where it lands, so the restore after the new page is laid out agrees with it.
        const scene = singlePageScene(sizes ?? [], zoom, index);
        const pageTop = scrollTopForPage(scene.boxes, 0, zoom);
        if (element === null || pageTop === null) return;
        const top = pageTop + offset;
        readingPlace.current = viewAnchor(scene.boxes, top);
        setSinglePage(index);
        scrollToScene(element, top, scene.height);
        setScroll(viewportOf(element, scene.height));
        return;
      }
      // Spreads aim at the page's own top like the continuous column (the desktop's go_to_page rule),
      // so one function serves both layouts here.
      const pageTop = scrollTopForPage(layout.boxes, index, zoom);
      if (element === null || pageTop === null) return;
      scrollToScene(element, pageTop + offset, layout.height);
      capturePlace();
      setScroll(viewportOf(element, layout.height));
    },
    [layout, sizes, zoom, viewLayout, capturePlace],
  );

  // Follows a link (W5.3): a jump to a page of this document, or a notice when it is not one. The
  // notice replaces an earlier one, and any link action clears it.
  const followLink = useCallback(
    (link: LinkDto) => {
      const action = classifyLink(link);
      if (action.action === 'notice') {
        setLinkNotice({ notice: action.notice, scheme: action.scheme });
        return;
      }
      setLinkNotice(null);
      if (action.action === 'confirm') {
        setLinkConfirm(action);
        return;
      }
      if (onLinkGoTo !== undefined) onLinkGoTo(action.page, action.top);
      else scrollToPage(action.page, action.top ?? undefined);
    },
    [onLinkGoTo, scrollToPage],
  );

  // The tap on a link: the release follows the link the press started on, at the list's index.
  const followPress = (press: LinkPress) => {
    const link = linkLists.ready(press.page, scheduler.revisionOf(press.page))?.[press.index];
    if (link !== undefined) followLink(link);
  };

  // Whether the links take presses now (W5.3): only under Select and Hand, with no gesture tool on.
  const linkLayerOn = linksActive({
    commentTool,
    viewTool: tool,
    markToolOn: redactionTools.state.active,
  });

  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);

  // Ctrl+wheel (Cmd on macOS) steps the zoom through the desktop's steps and keeps the point under
  // the pointer in place. The listener is native and not passive, so preventDefault stops the
  // browser from zooming the whole page.
  useEffect(() => {
    const element = scrollRef.current;
    if (element === null || sizes === null) return;
    const onWheel = (event: WheelEvent) => {
      if (!isZoomWheel(event)) {
        // Above the scaled-space limit a native scroll would move the spacer, not the scene, so the
        // wheel scrolls the spacer at the scene's rate instead (W5.1a S4). Below it, native scrolling.
        if (!isScaledSpace(contentHeight.current)) {
          scaledRemainder.current = 0;
          return;
        }
        event.preventDefault();
        const ratio = scrollPerScenePixel(
          contentHeight.current,
          viewportOf(element, contentHeight.current).height,
        );
        const step = takeWholePixels(scaledRemainder.current, wheelDeltaPixels(event) * ratio);
        scaledRemainder.current = step.remainder;
        element.scrollTop += step.whole;
        element.scrollLeft += wheelDeltaPixels({
          deltaY: event.deltaX,
          deltaMode: event.deltaMode,
        });
        return;
      }
      event.preventDefault();
      const step = takeWheelSteps(wheelPending.current, wheelDeltaPixels(event));
      wheelPending.current = step.pending;
      if (step.direction === null) return;
      const from = zoomRef.current;
      const to = stepZoom(from, step.direction);
      if (to === from) return;
      const rect = element.getBoundingClientRect();
      // The anchor is kept in scene coordinates: the layout after the zoom maps it back to a scroll position.
      pendingAnchor.current = anchoredScroll({
        scrollLeft: element.scrollLeft,
        scrollTop: viewportOf(element, contentHeight.current).top,
        pointerX: event.clientX - rect.left,
        pointerY: event.clientY - rect.top,
        fromZoom: from,
        toZoom: to,
      });
      zoomRef.current = to;
      setFit(null);
      setManualZoom(to);
    };
    element.addEventListener('wheel', onWheel, { passive: false });
    return () => element.removeEventListener('wheel', onWheel);
  }, [sizes]);

  // The arrow, page and space keys scroll the spacer; above the scaled-space limit they move it by the
  // scene's rate, as the wheel does (W5.1a S4). Home and End need nothing: both ends map to each other.
  // A key typed into a field or a control is left to it.
  const scrollByKey = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    const element = event.currentTarget;
    if (!isScaledSpace(contentHeight.current)) {
      scaledRemainder.current = 0;
      return;
    }
    const target = event.target;
    if (
      target instanceof HTMLElement &&
      (target.closest('input, textarea, select, [contenteditable]') ||
        isInsideControl(target, element))
    )
      return;
    const height = viewportOf(element, contentHeight.current).height;
    const px = keyScrollPixels(event.key, event.shiftKey, height);
    if (px === null) return;
    event.preventDefault();
    const before = element.scrollTop;
    const step = takeWholePixels(
      scaledRemainder.current,
      px * scrollPerScenePixel(contentHeight.current, height),
    );
    element.scrollTop = before + step.whole;
    // A browser stops the write at the end of the range: then the owed fraction points past the end
    // and is dropped, so the next key starts from the end and not from a stale fraction. A fractional
    // scrollTop (HiDPI) can differ from the whole pixels written by under 1 px with no clamp, so that
    // gap still counts as an unclamped move and keeps its remainder.
    const clamped = Math.abs(element.scrollTop - (before + step.whole)) >= 1;
    scaledRemainder.current = clamped ? 0 : step.remainder;
  };

  // Runs once a new layout (a zoom, a fit refit or a layout change) is in the DOM, so the scroll range
  // already has the new size. A wheel zoom restores the point under the pointer (its own anchor);
  // any other change restores the place the reader had (W5.1a S2).
  useLayoutEffect(() => {
    boxesOnScreen.current = layout.boxes;
    contentHeight.current = layout.height;
    const element = scrollRef.current;
    if (element === null) return;
    const anchor = pendingAnchor.current;
    if (anchor !== null) {
      pendingAnchor.current = null;
      element.scrollLeft = anchor.left;
      scrollToScene(element, anchor.top, layout.height);
      setScroll(viewportOf(element, layout.height));
      return;
    }
    const top =
      readingPlace.current === null ? null : scrollTopForAnchor(layout.boxes, readingPlace.current);
    if (top === null) return;
    scrollToScene(element, top, layout.height);
    setScroll(viewportOf(element, layout.height));
  }, [layout]);

  const devicePixelRatio = useDevicePixelRatio();
  const scale = scaleMilli(zoom, devicePixelRatio);
  const range = visibleRange(layout.boxes, scroll.top, scroll.height);
  // Above the scroll-space limit the stage is scrolled by the scene top, so a page's offset in the
  // stage is its scene top less that; below the limit the stage is the layout and offsets are none.
  const scaled = isScaledSpace(layout.height);
  const pageOffset = scaled ? scroll.top : 0;
  // The pages mounted: the window, plus the focused page when it lies outside it, so the page keeps
  // the keyboard focus (and a comment draft or a text span in it) as the reader scrolls on.
  const focusedBox =
    focusedPage === null ? undefined : layout.boxes.find((box) => box.index === focusedPage);
  const mounted = [...layout.boxes.slice(range.first, range.end)];
  if (focusedBox !== undefined && (focusedBox.index < range.first || focusedBox.index >= range.end))
    mounted.push(focusedBox);
  // The single-page layout shows one page, so the page at the viewport is that page wherever the
  // reader scrolls within it; the other layouts take the page the viewport's third falls in.
  const currentPage =
    (viewLayout === 'single'
      ? singlePage
      : currentPageIndex(layout.boxes, scroll.top, scroll.height)) + 1;
  useEffect(() => {
    topPage.current = currentPage - 1;
  }, [currentPage]);
  const pageCount = sizes?.length ?? 0;
  // A change to the page count (insert, delete, reorder) makes every page number name another page,
  // so the selection and the copies in progress are dropped (W5.4 section 4).
  const lastPageCount = useRef(pageCount);
  useEffect(() => {
    if (lastPageCount.current === pageCount) return;
    lastPageCount.current = pageCount;
    selectAllPending.current = null;
    modelSelection.current = null;
    selectModel(null);
    notifyCopies(copyWatchers.current, { kind: 'changed', pages: null });
  }, [pageCount, selectModel]);
  useEffect(() => {
    onViewChange?.({
      page: currentPage,
      zoom,
      layout: viewLayout,
      labels,
      pageCount,
      fit,
      firstAlone,
      night,
    });
  }, [currentPage, zoom, viewLayout, labels, pageCount, fit, firstAlone, night, onViewChange]);

  // Plans the page renders from what is on screen now: the visible pages first, then the pages
  // just outside the viewport (prefetch, which the scheduler sends after the visible ones). A page
  // above 4 MP is planned as its tiles. Renders finishing bump the tick, which runs this again, so
  // the queue keeps moving until it is empty.
  useEffect(() => {
    if (sizes === null) return;
    scheduler.plan(
      'page',
      planPageRenders({
        boxes: layout.boxes,
        sizes,
        first: range.first,
        end: range.end,
        scrollTop: scroll.top,
        viewportHeight: scroll.height,
        scaleMilli: scale,
      }),
    );
    // The plan depends on what is on screen and the scale; the scheduler is stable state.
  }, [sizes, layout, range.first, range.end, scroll.top, scroll.height, scale, scheduler, tick]);

  const total = pageCount;

  // The canvas pill's and the handle's zoom request: a fit mode or a manual zoom. Only setters are
  // used, so the callback is stable.
  const applyZoom = useCallback(
    (request: ZoomRequest) => {
      capturePlace();
      if ('fit' in request) {
        setFit(request.fit);
      } else {
        setFit(null);
        setManualZoom(clampZoom(request.zoom));
      }
    },
    [capturePlace],
  );
  // The next or previous page, clamped to the document (the pill's steps and the handle's).
  const stepPage = useCallback(
    (direction: -1 | 1) => {
      scrollToPage(Math.min(total - 1, Math.max(0, currentPage - 1 + direction)));
    },
    [scrollToPage, total, currentPage],
  );
  // Night reading is saved as the preference the next document opens with (W4.5), as the toolbar's
  // button does. Stable, so the handle does not change for it.
  const chooseNight = useCallback((on: boolean) => {
    setNight(on);
    writePreferences({ ...readPreferences(), nightReading: on });
  }, []);
  // Hooks stay above the load-failed return, so the handle is set before it.
  useImperativeHandle(
    ref,
    () => ({
      scrollToPage,
      revealRect,
      zoom: (request) =>
        applyZoom('step' in request ? { zoom: stepZoom(zoom, request.step) } : request),
      stepPage,
      focusPageField: () => {
        pageFieldRef.current?.focus();
        pageFieldRef.current?.select();
      },
      deleteEditSelection,
      setLayout: changeLayout,
      setNight: chooseNight,
      cachedLines: (page) => textLayers.ready(page, scheduler.revisionOf(page)),
      setCommentTool: chooseCommentTool,
      setRedacting: chooseRedacting,
      selectAll: () => {
        const last = pageCount - 1;
        if (last < 0) return;
        if (!selectingNow.current) {
          // Under Hand the browser's own selection holds the top page (the layer is not the model's).
          const page = topPage.current;
          selectPageTextLayer(
            window.getSelection(),
            scrollRef.current?.querySelector(`[data-page="${page}"] .page-text-layer`) ?? null,
          );
          return;
        }
        // The whole document, from its first caret to the end of the last page (the desktop's
        // DocumentView.select_all). It needs only the last page's length (W5.4 section 2.2).
        const selectWhole = (length: number): void => {
          selectModel(new TextSelection({ page: 0, index: 0 }, { page: last, index: length }));
          toasts.push('info', tp('text.selectedAll', pageCount));
        };
        selectAllPending.current = null;
        const revision = scheduler.revisionOf(last);
        const index = textLayers.readyIndex(last, revision);
        if (index !== undefined) {
          selectWhole(index.length);
          return;
        }
        // The last page's length is still loading. The selection is set when it arrives, unless the
        // user has dragged, cleared or changed the document in the meantime (the pending ref changes).
        const pending = { page: last, revision };
        selectAllPending.current = pending;
        void textLayers.index(last, revision).then(
          (loaded) => {
            if (selectAllPending.current !== pending) return;
            selectAllPending.current = null;
            if (selectingNow.current) selectWhole(loaded.length);
          },
          () => {
            if (selectAllPending.current !== pending) return;
            selectAllPending.current = null;
            toasts.push('info', t('text.stillLoading'));
          },
        );
      },
      copyPlan: (): CopyPlan => {
        const sel = modelSelection.current;
        const from = sel?.start.page ?? 0;
        const to = sel?.end.page ?? -1;
        return planCopy(
          selectingNow.current,
          sel,
          (page) => textLayers.readyIndex(page, scheduler.revisionOf(page)),
          selectAllPending.current !== null,
          (listener) => watchRange(copyWatchers.current, from, to, listener),
        );
      },
      selectedPageSpan: (): number => {
        const sel = selectingNow.current ? modelSelection.current : null;
        return sel === null || sel.isEmpty ? 0 : sel.end.page - sel.start.page + 1;
      },
      selectedAreas: (): ModelRead<PageArea[]> => {
        if (!selectingNow.current) return MODEL_OFF;
        const sel = modelSelection.current;
        if (sel === null || sel.isEmpty) return { state: 'ready', value: [] };
        const read = readModelIndexes(sel, (page) =>
          textLayers.readyIndex(page, scheduler.revisionOf(page)),
        );
        if (read.state !== 'ready') return read;
        return { state: 'ready', value: modelSelectionAreas(sel, read.value) };
      },
      clearTextSelection: () => {
        selectAllPending.current = null;
        modelSelection.current = null;
        selectModel(null);
      },
      loadTextPages: (pages) =>
        Promise.all(
          pages.slice(0, MAX_TEXT_LAYERS).map((page) =>
            textLayers.index(page, scheduler.revisionOf(page)).then(
              () => undefined,
              () => undefined,
            ),
          ),
        ).then(() => undefined),
    }),
    [
      scrollToPage,
      revealRect,
      zoom,
      applyZoom,
      stepPage,
      textLayers,
      scheduler,
      deleteEditSelection,
      chooseCommentTool,
      chooseRedacting,
      changeLayout,
      chooseNight,
      selectModel,
      pageCount,
    ],
  );

  if (loadFailed) {
    return (
      <p role="alert" className="doc-view-status">
        {t('view.loadFailed')}
      </p>
    );
  }

  const commentDeps = { rpc, handle, scheduler };
  const place = (page: number, rect: PageRect, text: string) => {
    if (commentTool !== null) void placeComment(commentDeps, commentTool, page, rect, text);
  };
  // A shape gesture (W6.7b): the engine adds it, and on success the tool returns to Select (one
  // shape per choice). A failure keeps the tool on so the user can retry. While an add is in
  // flight, a further gesture or Enter does nothing, so a double Enter adds one shape.
  const shapeDeps = { rpc, handle, cache: editObjects, scheduler };
  const drawShape = (page: number, spec: ShapeSpec | null) => {
    if (shapeInFlight.current) return;
    if (spec === null) {
      noteShapeTooSmall();
      return;
    }
    // The tab is read now, before the add starts: the user may switch tabs while it is in flight.
    const drawnOn = tabId;
    shapeInFlight.current = true;
    void addShape(shapeDeps, page, spec).then((added) => {
      shapeInFlight.current = false;
      if (added) onToolChange?.('select', drawnOn);
    });
  };

  return (
    <section
      className={[
        'doc-view',
        night ? 'doc-view--night' : '',
        tool === 'hand' ? 'doc-view--hand' : '',
        tool === 'edit' ? 'doc-view--edit' : '',
        tool === 'cropBox' ? 'doc-view--crop-box' : '',
        pan.panning ? 'doc-view--panning' : '',
      ]
        .filter((name) => name !== '')
        .join(' ')}
      aria-label={t('view.label')}
    >
      {tool === 'cropBox' ? <p className="crop-draw-hint">{t('crop.drawHint')}</p> : null}
      <div className="doc-view-toolbar" role="toolbar" aria-label={t('view.label')}>
        <div role="group" aria-label={t('view.layoutLabel')}>
          <button
            type="button"
            aria-pressed={viewLayout === 'continuous'}
            onClick={() => changeLayout({ layout: 'continuous', firstAlone })}
          >
            {t('view.layoutSingle')}
          </button>
          <button
            type="button"
            aria-pressed={viewLayout === 'single'}
            onClick={() => changeLayout({ layout: 'single', firstAlone })}
          >
            {t('view.layoutPage')}
          </button>
          <button
            type="button"
            aria-pressed={viewLayout === 'twoPage'}
            onClick={() => changeLayout({ layout: 'twoPage', firstAlone })}
          >
            {t('view.layoutTwoPage')}
          </button>
        </div>
        <button
          type="button"
          aria-pressed={firstAlone}
          disabled={viewLayout !== 'twoPage'}
          onClick={() => changeLayout({ layout: viewLayout, firstAlone: !firstAlone })}
        >
          {t('view.coverAlone')}
        </button>
        <button type="button" aria-pressed={night} onClick={() => chooseNight(!night)}>
          {t('view.night')}
        </button>
        <CommentToolbar tool={commentTool} onChoose={chooseCommentTool} />
        <RedactionBar
          state={redactionTools.state}
          onToggle={() => chooseRedacting(!redactionTools.state.active)}
          onClear={() => void redactionTools.controller.clear()}
          onApply={() => void redactionTools.controller.apply(null)}
        />
      </div>
      {linkNotice === null ? null : (
        <p role="status" className="doc-link-notice">
          {linkNoticeText(linkNotice)}
        </p>
      )}
      <LinkConfirmDialog target={linkConfirm} onClose={() => setLinkConfirm(null)} />
      {sizes === null ? (
        <p role="status" className="doc-view-status">
          {t('view.loading')}
        </p>
      ) : (
        <div
          ref={scrollRef}
          className="doc-view-scroll"
          tabIndex={0}
          aria-label={t('view.pages')}
          onScroll={measure}
          onKeyDown={scrollByKey}
          onFocus={(event) => {
            // Focus inside a page pins that page; focus outside the pages (the pill, the scroller) unpins it.
            const page =
              event.target instanceof Element
                ? event.target.closest<HTMLElement>('.doc-page')
                : null;
            setFocusedPage(page === null ? null : Number(page.dataset.page));
          }}
          onPointerDown={(event) => {
            linkPress.current = linkPressAt(event);
            // A press starts a new selection (or pans): a Select All still waiting for its length is dropped.
            selectAllPending.current = null;
            if (!textSelection.startPress(event)) pan.onPointerDown(event);
          }}
          onPointerMove={pan.onPointerMove}
          onPointerUp={(event) => {
            pan.onPointerUp(event);
            // The pan's capture sends the click to the scroller, so a link is followed here: a tap
            // on the press's link, not a drag (linkTap.ts).
            const press = linkPress.current;
            linkPress.current = null;
            if (press !== null && isTap(press, event.pointerId, event.clientX, event.clientY))
              followPress(press);
          }}
          onPointerCancel={(event) => {
            linkPress.current = null;
            pan.onPointerCancel(event);
          }}
          onLostPointerCapture={pan.onLostPointerCapture}
        >
          <div
            className="doc-view-content"
            style={{ width: layout.width, height: spacerHeight(layout.height) }}
          >
            {/* The stage holds the pages. Above the limit it sticks to the top of the scroller at the
                viewport's height, and the pages sit at their scene position less the scene top, so the
                pages stay in place while the spacer is shorter than the layout (W5.1a S4). */}
            <div
              className={scaled ? 'doc-view-stage doc-view-stage--scaled' : 'doc-view-stage'}
              style={{ height: scaled ? scroll.height : layout.height }}
            >
              {mounted.map((box) => {
                // A page above 4 MP is drawn as the tiles of its visible part; a smaller one as one image.
                const size = sizes[box.index];
                const tiled =
                  size === undefined
                    ? null
                    : tilesInViewport(box, size, scale, scroll.top, scroll.height);
                const keys =
                  tiled === null
                    ? [scheduler.cacheKeyOf('page', box.index, scale)]
                    : tiled.cells.map((cell) =>
                        scheduler.cacheKeyOf('page', box.index, scale, cell),
                      );
                return (
                  <div
                    key={box.index}
                    data-page={box.index}
                    className="doc-page"
                    role="group"
                    aria-label={t('view.pageNumber', { page: box.index + 1, total })}
                    style={{
                      top: box.top - pageOffset,
                      left: box.left,
                      width: box.width,
                      height: box.height,
                    }}
                  >
                    <div className="doc-page-placeholder" />
                    {tiled === null ? (
                      <CachedCanvas
                        cache={scheduler.cache}
                        cacheKey={keys[0] ?? ''}
                        version={tick}
                        className="doc-page-canvas"
                      />
                    ) : (
                      tiled.cells.map((cell, index) => {
                        const rect = tileRect(tiled.device, cell);
                        const percent = (value: number, of: number) => `${(value / of) * 100}%`;
                        return (
                          <CachedCanvas
                            key={`${cell.tileX}:${cell.tileY}`}
                            cache={scheduler.cache}
                            cacheKey={keys[index] ?? ''}
                            version={tick}
                            className="doc-page-canvas"
                            style={{
                              left: percent(rect.x, tiled.device.width),
                              top: percent(rect.y, tiled.device.height),
                              width: percent(rect.width, tiled.device.width),
                              height: percent(rect.height, tiled.device.height),
                            }}
                          />
                        );
                      })
                    )}
                    <PageMarks rects={marksOnPage(searchMarks, box.index, zoom)} />
                    {commentTool !== null && sizes !== null ? (
                      <CommentLayer
                        tool={commentTool}
                        zoom={zoom}
                        widthPt={sizes[box.index]?.width ?? 0}
                        heightPt={sizes[box.index]?.height ?? 0}
                        onPlace={(rect, text) => place(box.index, rect, text)}
                      />
                    ) : null}
                    <SelectedMarkOutlines
                      rects={selectedRectsOn(redactionTools.state, box.index)}
                      zoom={zoom}
                    />
                    {tool === 'edit' && sizes !== null ? (
                      <EditLayer
                        cache={editObjects}
                        page={box.index}
                        revision={scheduler.revisionOf(box.index)}
                        zoom={zoom}
                        selection={editSelection}
                        onSelect={setEditSelection}
                        onCommit={commitChange}
                      />
                    ) : null}
                    {isShapeTool(tool) && sizes !== null ? (
                      <ShapeLayer
                        kind={SHAPE_KIND[tool]}
                        zoom={zoom}
                        widthPt={sizes[box.index]?.width ?? 0}
                        heightPt={sizes[box.index]?.height ?? 0}
                        focusable={box.index === currentPage}
                        onDraw={(spec) => drawShape(box.index, spec)}
                        onStop={() => onToolChange?.('select')}
                      />
                    ) : null}
                    {tool === 'cropBox' && sizes !== null ? (
                      <CropLayer
                        zoom={zoom}
                        widthPt={sizes[box.index]?.width ?? 0}
                        heightPt={sizes[box.index]?.height ?? 0}
                        onDraw={(rect) => {
                          const size = sizes[box.index];
                          if (size !== undefined)
                            onCropBoxEnd?.({
                              page: box.index + 1,
                              margins: marginsFromBox(rect, size),
                            });
                        }}
                      />
                    ) : null}
                    {redactionTools.state.active && sizes !== null ? (
                      <RedactLayer
                        zoom={zoom}
                        widthPt={sizes[box.index]?.width ?? 0}
                        heightPt={sizes[box.index]?.height ?? 0}
                        onPlace={(rect) => void redactionTools.controller.add(box.index, rect)}
                      />
                    ) : null}
                    <PageTextLayer
                      cache={textLayers}
                      page={box.index}
                      revision={scheduler.revisionOf(box.index)}
                      zoom={zoom}
                      passive={textLayerPassive({
                        commentTool,
                        viewTool: tool,
                        markToolOn: redactionTools.state.active,
                      })}
                      selection={selecting ? textSelection.selection : undefined}
                    />
                    {linkLayerOn && sizes !== null ? (
                      <PageLinkLayer
                        cache={linkLists}
                        page={box.index}
                        revision={scheduler.revisionOf(box.index)}
                        zoom={zoom}
                        labels={labels}
                        onFollow={followLink}
                      />
                    ) : null}
                    {keys.some((key) => scheduler.isFailed(key)) ? (
                      <span className="doc-page-error">{t('view.renderFailed')}</span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
      {sizes === null ? null : (
        <CanvasPill
          page={currentPage}
          total={total}
          labels={labels}
          zoom={zoom}
          fit={fit}
          onGoTo={scrollToPage}
          onStep={stepPage}
          onZoom={applyZoom}
          onZoomStep={(direction) => zoomTo(stepZoom(zoom, direction))}
          pageFieldRef={pageFieldRef}
        />
      )}
    </section>
  );
}
