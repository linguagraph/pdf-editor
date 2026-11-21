import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PageRpc } from '../../engine/engineConnection';
import type { CommentDto, CommentListResult, CommentThreadDto } from '../../rpc/generated';
import { CommentsController, type CommentsDeps } from './controller';
import { NO_FILTER } from './filters';

function comment(page: number, author: string, contents: string): CommentDto {
  return {
    page,
    name: `${author}${page}`,
    type: 'Text',
    author,
    contents,
    subject: '',
    created: null,
    modified: null,
    state: 'None',
    locked: false,
    rect: { x0: 0, y0: 0, x1: 10, y1: 10 },
  };
}

function thread(page: number, author: string, replies: CommentDto[] = []): CommentThreadDto {
  return { comment: comment(page, author, `${author} says`), replies, status: 'None' };
}

function reply(threads: CommentThreadDto[], truncated = false): CommentListResult {
  return { threads, truncated };
}

const ANA = thread(0, 'Ana', [comment(0, 'Ben', 'Reply')]);
const CARL = thread(1, 'Carl');

interface Harness {
  readonly controller: CommentsController;
  readonly call: ReturnType<typeof vi.fn>;
  // Tells the controller a page revision moved.
  readonly revisionChanged: () => void;
  readonly subscribers: () => number;
  readonly stop: () => void;
}

function harness(overrides: Partial<CommentsDeps> = {}): Harness {
  const call = vi.fn(async () => reply([ANA, CARL]));
  const listeners = new Set<(pages: readonly number[]) => void>();
  const scheduler = {
    subscribeRevisions: (listener: (pages: readonly number[]) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  const controller = new CommentsController({
    rpc: { call } as unknown as PageRpc,
    handle: 'doc-1',
    scheduler,
    ...overrides,
  });
  const stop = controller.start();
  return {
    controller,
    call,
    stop,
    revisionChanged: () => {
      for (const listener of [...listeners]) listener([]);
    },
    subscribers: () => listeners.size,
  };
}

describe('CommentsController', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reads comments.list for its document when the panel opens', async () => {
    const { controller, call } = harness();
    controller.setOpen(true);
    await vi.waitFor(() => expect(controller.getSnapshot().loaded).toBe(true));
    expect(call).toHaveBeenCalledWith('comments.list', { handle: 'doc-1' });
    expect(controller.getSnapshot().threads).toEqual([ANA, CARL]);
    expect(controller.getSnapshot().visible).toEqual([ANA, CARL]);
    expect(controller.getSnapshot().options.authors).toEqual(['Ana', 'Ben', 'Carl']);
  });

  it('reads the list again on a revision change while open, once for a burst', async () => {
    const { controller, call, revisionChanged } = harness();
    controller.setOpen(true);
    await controller.reload();
    call.mockClear();
    revisionChanged();
    revisionChanged();
    revisionChanged();
    expect(call).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(200);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('reads on undo, redo and save as well as on a revision', async () => {
    const { controller, call } = harness();
    controller.setOpen(true);
    await controller.reload();
    for (const change of ['undo', 'redo', 'saved'] as const) {
      call.mockClear();
      controller.noteChange();
      await vi.advanceTimersByTimeAsync(200);
      expect(call, change).toHaveBeenCalledTimes(1);
    }
  });

  it('does not read on a change while closed; it reads once on open', async () => {
    const { controller, call, revisionChanged } = harness();
    revisionChanged();
    await vi.advanceTimersByTimeAsync(500);
    expect(call).not.toHaveBeenCalled();
    controller.setOpen(true);
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1));
    controller.setOpen(false);
    controller.setOpen(true);
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1));
  });

  it('keeps the subscription to revisions until the stop, and drops a pending read on stop', async () => {
    const { controller, call, revisionChanged, subscribers, stop } = harness();
    expect(subscribers()).toBe(1);
    controller.setOpen(true);
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1));
    call.mockClear();
    revisionChanged();
    stop();
    expect(subscribers()).toBe(0);
    await vi.advanceTimersByTimeAsync(500);
    expect(call).not.toHaveBeenCalled();
  });

  it('filters the visible threads, and clears the filter', async () => {
    const { controller } = harness();
    controller.setOpen(true);
    await controller.reload();
    controller.setFilter({ author: 'Ben' });
    expect(controller.getSnapshot().visible).toEqual([ANA]);
    expect(controller.getSnapshot().filter.author).toBe('Ben');
    controller.setFilter({ search: 'carl' });
    expect(controller.getSnapshot().visible).toEqual([]);
    controller.clearFilter();
    expect(controller.getSnapshot().filter).toEqual(NO_FILTER);
    expect(controller.getSnapshot().visible).toEqual([ANA, CARL]);
  });

  it('keeps a filter choice in the options after a reload drops its thread', async () => {
    const { controller, call } = harness();
    controller.setOpen(true);
    await controller.reload();
    controller.setFilter({ author: 'Carl' });
    call.mockResolvedValueOnce(reply([ANA]));
    await controller.reload();
    expect(controller.getSnapshot().options.authors).toContain('Carl');
    expect(controller.getSnapshot().visible).toEqual([]);
  });

  it('reports the truncated flag from the engine', async () => {
    const { controller, call } = harness();
    call.mockResolvedValue(reply([ANA], true));
    controller.setOpen(true);
    await controller.reload();
    expect(controller.getSnapshot().truncated).toBe(true);
    expect(controller.getSnapshot().loaded).toBe(true);
  });

  it('drops a reply that is older than the one already applied', async () => {
    const { controller, call } = harness();
    const resolvers: Array<(value: CommentListResult) => void> = [];
    call.mockImplementation(() => new Promise((resolve) => resolvers.push(resolve)));
    const first = controller.reload();
    const second = controller.reload();
    resolvers[1]?.(reply([CARL]));
    await second;
    resolvers[0]?.(reply([ANA]));
    await first;
    expect(controller.getSnapshot().threads).toEqual([CARL]);
  });

  it('keeps the list it had when a read fails', async () => {
    const { controller, call } = harness();
    controller.setOpen(true);
    await controller.reload();
    call.mockRejectedValueOnce(new Error('closed'));
    await controller.reload();
    expect(controller.getSnapshot().threads).toEqual([ANA, CARL]);
    expect(controller.getSnapshot().loaded).toBe(true);
  });
});
