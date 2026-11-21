import { describe, expect, it } from 'vitest';
import type { ProbeState } from './probe';
import { mergePreview } from './preview';
import type { SourceItem } from './sources';

function row(id: number, name: string, pages = ''): SourceItem {
  return { id, name, file: new File(['x'], name), pages };
}

const counted = (pages: number): ProbeState => ({ kind: 'counted', pages, encrypted: false });

describe('mergePreview', () => {
  it('counts an image as one page and places every row in list order', () => {
    const items = [row(1, 'a.pdf'), row(2, 'scan.png'), row(3, 'b.pdf')];
    const probes = new Map<number, ProbeState>([
      [1, counted(3)],
      [3, counted(2)],
    ]);
    const preview = mergePreview(items, probes);

    expect(preview.rows).toEqual([
      { id: 1, take: 3, first: 1, last: 3 },
      { id: 2, take: 1, first: 4, last: 4 },
      { id: 3, take: 2, first: 5, last: 6 },
    ]);
    expect(preview.total).toBe(6);
    expect(preview.unknown).toBe(0);
  });

  it('takes only the pages a range keeps, and every page of a blank or "all" box', () => {
    const items = [row(1, 'a.pdf', '2-3, 5'), row(2, 'b.pdf', 'all'), row(3, 'c.pdf')];
    const probes = new Map<number, ProbeState>([
      [1, counted(6)],
      [2, counted(2)],
      [3, counted(1)],
    ]);
    const preview = mergePreview(items, probes);

    expect(preview.rows.map((r) => r.take)).toEqual([3, 2, 1]);
    expect(preview.rows[1]).toEqual({ id: 2, take: 2, first: 4, last: 5 });
    expect(preview.total).toBe(6);
  });

  it('a Pages box that does not fit gives its row no pages, and no total', () => {
    const items = [row(1, 'a.pdf', '9'), row(2, 'b.pdf')];
    const probes = new Map<number, ProbeState>([
      [1, counted(2)],
      [2, counted(2)],
    ]);
    const preview = mergePreview(items, probes);

    expect(preview.rows[0]).toEqual({ id: 1, take: null, first: null, last: null });
    expect(preview.total).toBeNull();
    expect(preview.unknown).toBe(0);
  });

  it('stops the placement at the first unknown row, and counts the files not yet counted', () => {
    const items = [row(1, 'a.pdf'), row(2, 'b.pdf'), row(3, 'c.pdf')];
    const probes = new Map<number, ProbeState>([
      [1, counted(2)],
      [2, { kind: 'pending' }],
      [3, counted(4)],
    ]);
    const preview = mergePreview(items, probes);

    expect(preview.rows[0]).toEqual({ id: 1, take: 2, first: 1, last: 2 });
    expect(preview.rows[1]).toEqual({ id: 2, take: null, first: null, last: null });
    expect(preview.rows[2]).toEqual({ id: 3, take: 4, first: null, last: null });
    expect(preview.total).toBeNull();
    expect(preview.known).toBe(6);
    expect(preview.unknown).toBe(1);
  });

  it('a locked or unreadable PDF is not counted until it is', () => {
    const items = [row(1, 'a.pdf'), row(2, 'b.pdf')];
    const probes = new Map<number, ProbeState>([
      [1, { kind: 'locked' }],
      [2, { kind: 'unreadable' }],
    ]);
    const preview = mergePreview(items, probes);

    expect(preview.unknown).toBe(2);
    expect(preview.total).toBeNull();
  });
});
