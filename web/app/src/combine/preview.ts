// The merged result's preview (W6.3b, PR 1): how many pages the combined file will have, and which of
// them each row's pages become. Pure: no DOM, no engine, no call. The count and the order follow from
// the per-file counts and the page-range rule, so nothing is merged to show them, and the preview
// costs nothing to recompute on each change to the list.
import { parsePageRanges } from '../pages/pageRanges';
import type { ProbeState } from './probe';
import { isImageSourceName, type SourceItem } from './sources';

export interface PreviewRow {
  readonly id: number;
  // The pages this row adds to the result; null while they are not known.
  readonly take: number | null;
  // The 1-based pages of the result this row takes. Null once anything before it is unknown.
  readonly first: number | null;
  readonly last: number | null;
}

export interface MergePreview {
  readonly rows: readonly PreviewRow[];
  // The pages of the whole result; null while any row's pages are not known.
  readonly total: number | null;
  // The pages of the rows whose count is known, so far.
  readonly known: number;
  // The PDF rows whose file has not been counted (still pending, unchecked, locked or unreadable).
  readonly unknown: number;
}

// The pages one row adds, or null. An image is one page. A PDF row needs its count; its Pages text is
// read in full mode, so a box that does not fit gives no pages.
function takeOf(item: SourceItem, state: ProbeState | undefined): number | null {
  if (isImageSourceName(item.name)) return 1;
  if (state?.kind !== 'counted') return null;
  const parsed = parsePageRanges(item.pages, state.pages);
  return parsed.ok && parsed.pages !== null ? parsed.pages.length : null;
}

export function mergePreview(
  items: readonly SourceItem[],
  probes: ReadonlyMap<number, ProbeState>,
): MergePreview {
  const rows: PreviewRow[] = [];
  let placed = 0; // the result pages the rows before this one take
  let placing = true; // every row so far has a known take
  let known = 0;
  let unknown = 0;
  for (const item of items) {
    const state = probes.get(item.id);
    const take = takeOf(item, state);
    if (take === null) {
      placing = false;
      if (!isImageSourceName(item.name) && state?.kind !== 'counted') unknown += 1;
      rows.push({ id: item.id, take: null, first: null, last: null });
      continue;
    }
    known += take;
    const first = placing ? placed + 1 : null;
    const last = placing ? placed + take : null;
    rows.push({ id: item.id, take, first, last });
    placed += take;
  }
  return { rows, total: placing ? placed : null, known, unknown };
}
