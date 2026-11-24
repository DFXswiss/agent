// U-WEB-ui-tasks (volume, ADR-086 section 1): the task lists render a window of at most `MAX_ROWS` rows with
// "older"/"newer" and "Load more" only on request; the rows read titles and owners through one selector per list and
// are memoized on their content; the title search loads at most `SCAN_PAGES` further pages per request and says how
// many tasks it searched ("Search further").
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import type { TaskRow } from '../../src/core/store/slices/tasks';
import { I18nProvider } from '../../src/ui/i18n';
import { sameTitle, selectRowInfo } from '../../src/ui/screens/tasks/hooks';
import { OverviewScreen } from '../../src/ui/screens/tasks/OverviewScreen';
import { SCAN_PAGES } from '../../src/ui/screens/tasks/scan';
import { sameTaskItem, type TaskItemData } from '../../src/ui/screens/tasks/TaskItem';
import { TaskListScreen } from '../../src/ui/screens/tasks/TaskListScreen';
import { MAX_ROWS, ScanStatus } from '../../src/ui/screens/tasks/TaskTable';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';

function mount(t: TaskWorld, path: string, element: ReactNode, route: string) {
  const router = createMemoryRouter([{ path: route, element }], { initialEntries: [path] });
  const app = { store: t.store, tasks: t.context } as never;
  render(
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n('en')}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
}

const time = (n: number) => new Date(Date.UTC(2026, 10, 21, 12) - n * 60_000).toISOString();
const id = (n: number) => `task-${String(n).padStart(4, '0')}`;
const rowsOf = (t: TaskWorld, from: number, count: number) =>
  Array.from({ length: count }, (_, i) => taskRow(t, { task_id: id(from + i), last_activity_at: time(from + i) }));

function meta(t: TaskWorld, seq: number, task: string, title: string): EventEntry {
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: seq,
    receivedAt: null,
    event: {},
    status: 'verified',
    code: null,
    header: { id: `meta-${seq}`, kind: 'task.meta', task, author: t.me.id, ts: '2026-11-21T12:00:00.000Z' },
    body: { title },
  };
}

const before = (target: string) => new URLSearchParams(target.split('?')[1]).get('before');
const shown = () => screen.queryAllByTestId('tasks.row');
const titles = () => screen.queryAllByTestId('tasks.row.title').map((node) => node.textContent);
const pages = (t: TaskWorld) =>
  t.client.of(`GET /v1/workgroups/${t.wg}/tasks`).filter((call) => before(call.target) !== null).length;

describe('task list window', () => {
  it('renders at most MAX_ROWS of 500 loaded tasks and offers "Load more" only at the loaded end', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) =>
      before(target) === null
        ? { tasks: rowsOf(t, 0, 500), next_before: 'next' }
        : { tasks: rowsOf(t, 500, 50), next_before: null },
    );
    mount(t, `/o/${t.org}/w/${t.wg}`, <TaskListScreen />, '/o/:org/w/:wg');
    await waitFor(() => expect(shown()).toHaveLength(100));
    expect(screen.getByTestId('tasks.list')).toHaveAttribute('data-total', '500');
    expect(screen.getByTestId('tasks.list.older')).toHaveTextContent('400 more entries – show');
    expect(screen.queryByTestId('tasks.more')).toBeNull();
    expect(shown()[0]).toHaveAttribute('data-task', id(0));
    let clicks = 0;
    while (screen.queryByTestId('tasks.list.older') !== null) {
      fireEvent.click(screen.getByTestId('tasks.list.older'));
      clicks += 1;
      expect(shown().length).toBeLessThanOrEqual(MAX_ROWS);
    }
    expect(clicks).toBe(8);
    expect(shown()).toHaveLength(MAX_ROWS);
    expect(screen.getByTestId('tasks.list.newer')).toHaveTextContent('300 newer entries – show');
    expect(pages(t)).toBe(0);
    fireEvent.click(screen.getByTestId('tasks.more'));
    await waitFor(() => expect(shown().at(-1)).toHaveAttribute('data-task', id(549)));
    expect(shown()).toHaveLength(MAX_ROWS);
    expect(pages(t)).toBe(1);
    expect(screen.queryByTestId('tasks.more')).toBeNull();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = vi.fn();
    try {
      fireEvent.click(screen.getByTestId('tasks.list.newest'));
      expect(shown()[0]).toHaveAttribute('data-task', id(0));
      expect(shown()).toHaveLength(100);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});

describe('bounded title search', () => {
  it(`loads at most ${SCAN_PAGES} further pages per request and says how many tasks it searched`, async () => {
    const t = await taskWorld();
    const total = 30;
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) => {
      const page = Number(before(target) ?? 0);
      return { tasks: rowsOf(t, page, 1), next_before: page + 1 < total ? String(page + 1) : null };
    });
    putEvents(
      t.store,
      t.wg,
      Array.from({ length: total }, (_, n) => meta(t, n + 1, id(n), n === 25 ? 'Match 25' : `Other ${n}`)),
    );
    mount(t, `/o/${t.org}/tasks`, <OverviewScreen />, '/o/:org/tasks');
    await waitFor(() => expect(titles()).toEqual(['Other 0']));
    expect(screen.getByTestId('tasks.more')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('tasks.search'), { target: { value: 'match' } });
    expect(screen.getByTestId('tasks.list-loading')).toBeInTheDocument();
    expect(screen.getByTestId('tasks.table')).toHaveAttribute('aria-busy', 'true');
    const status = await screen.findByTestId('tasks.search.status');
    expect(pages(t)).toBe(SCAN_PAGES);
    expect(status).toHaveTextContent('Searched the 11 most recent tasks.');
    expect(screen.getByTestId('tasks.empty')).toHaveTextContent('No task matches the filters.');
    expect(screen.queryByTestId('tasks.list-loading')).toBeNull();
    fireEvent.click(screen.getByTestId('tasks.search.further'));
    await waitFor(() =>
      expect(screen.getByTestId('tasks.search.status')).toHaveTextContent('Searched the 21 most recent tasks.'),
    );
    expect(pages(t)).toBe(2 * SCAN_PAGES);
    fireEvent.click(screen.getByTestId('tasks.search.further'));
    await waitFor(() => expect(titles()).toEqual(['Match 25']));
    await waitFor(() => expect(pages(t)).toBe(total - 1));
    expect(screen.queryByTestId('tasks.search.status')).toBeNull();
    expect(screen.queryByTestId('tasks.more')).toBeNull();
  });

  it('stops at a page of matches, waits for a running load and drops a replaced request', async () => {
    const t = await taskWorld();
    let release: (value: unknown) => void = () => undefined;
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) =>
      before(target) === null
        ? { tasks: rowsOf(t, 0, 60), next_before: 'next' }
        : new Promise((resolve) => (release = resolve)),
    );
    putEvents(
      t.store,
      t.wg,
      Array.from({ length: 61 }, (_, n) => meta(t, n + 1, id(n), `Match ${n}`)),
    );
    mount(t, `/o/${t.org}/tasks`, <OverviewScreen />, '/o/:org/tasks');
    await waitFor(() => expect(shown()).toHaveLength(60));
    fireEvent.change(screen.getByTestId('tasks.search'), { target: { value: 'match' } });
    // 60 matches are more than a page: nothing is loaded, the status offers to search further
    expect(await screen.findByTestId('tasks.search.status')).toHaveTextContent('Searched the 60 most recent tasks.');
    expect(pages(t)).toBe(0);
    expect(screen.queryByTestId('tasks.more')).toBeNull();
    fireEvent.click(screen.getByTestId('tasks.search.further'));
    await waitFor(() => expect(pages(t)).toBe(1));
    expect(screen.queryByTestId('tasks.search.status')).toBeNull();
    expect(screen.getByTestId('tasks.table')).toHaveAttribute('aria-busy', 'true');
    // a new search text replaces the request while its page is loading: the new one waits for that load
    fireEvent.change(screen.getByTestId('tasks.search'), { target: { value: 'match 6' } });
    expect(titles()).toEqual(['Match 6']);
    await act(async () => release({ tasks: rowsOf(t, 60, 1), next_before: null }));
    await waitFor(() => expect(titles()).toEqual(['Match 6', 'Match 60']));
    expect(pages(t)).toBe(1);
    expect(screen.queryByTestId('tasks.search.status')).toBeNull();
  });

  it('ends a request at a failed page and shows the error instead of the status', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) => {
      if (before(target) !== null) throw new HubError('unavailable', 503);
      return { tasks: rowsOf(t, 0, 1), next_before: 'next' };
    });
    mount(t, `/o/${t.org}/tasks`, <OverviewScreen />, '/o/:org/tasks');
    await waitFor(() => expect(shown()).toHaveLength(1));
    fireEvent.click(screen.getByTestId('tasks.filter.mine'));
    expect(await screen.findByTestId('tasks.error')).toBeInTheDocument();
    expect(pages(t)).toBe(1);
    expect(screen.queryByTestId('tasks.search.status')).toBeNull();
  });

  it('names the total when the hub says it, and nothing without a further page', () => {
    const searchFurther = vi.fn();
    const scan = { scanned: 1200, total: 5000, hasMore: true, loading: false, searchFurther };
    const { rerender } = render(
      <I18nProvider value={createI18n('de')}>
        <ScanStatus scan={scan} />
      </I18nProvider>,
    );
    expect(screen.getByTestId('tasks.search.status')).toHaveTextContent('1.200 von 5.000 Tasks durchsucht.');
    fireEvent.click(screen.getByTestId('tasks.search.further'));
    expect(searchFurther).toHaveBeenCalledTimes(1);
    rerender(
      <I18nProvider value={createI18n('de')}>
        <ScanStatus scan={{ ...scan, hasMore: false }} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId('tasks.search.status')).toBeNull();
  });
});

describe('rows', () => {
  it('reads titles and owners of a list with one selector that keeps its map while nothing changed', async () => {
    const t = await taskWorld();
    const rows = [taskRow(t, { task_id: 'a' }), taskRow(t, { task_id: 'b' })] as TaskRow[];
    const select = selectRowInfo(t.org, rows);
    const first = select(t.store.getState());
    expect(first.get('a')).toEqual({ title: { status: 'missing' }, owner: null });
    putEvents(t.store, t.org, [{ ...meta(t, 1, 'x', 'x'), scopeId: t.org }]);
    expect(select(t.store.getState())).toBe(first);
    putEvents(t.store, t.wg, [meta(t, 1, 'a', 'Alpha')]);
    const second = select(t.store.getState());
    expect(second).not.toBe(first);
    expect(second.get('a')).toEqual({ title: { status: 'verified', text: 'Alpha' }, owner: t.me.id });
    putEvents(t.store, t.wg, [meta(t, 2, 'z', 'Other task')]);
    expect(select(t.store.getState())).toBe(second);
  });

  it('compares titles and row items by content', () => {
    const verified = { status: 'verified', text: 'A' } as const;
    const unverifiable = { status: 'unverifiable', code: 'signature_invalid' } as const;
    const missing = { status: 'missing' } as const;
    expect(sameTitle(verified, { status: 'verified', text: 'A' })).toBe(true);
    expect(sameTitle(verified, { status: 'verified', text: 'B' })).toBe(false);
    expect(sameTitle(unverifiable, { status: 'unverifiable', code: 'signature_invalid' })).toBe(true);
    expect(sameTitle(unverifiable, { status: 'unverifiable', code: 'other' })).toBe(false);
    expect(sameTitle(missing, { status: 'missing' })).toBe(true);
    expect(sameTitle(missing, verified)).toBe(false);
    const row = { task_id: 'a' } as TaskRow;
    const item: TaskItemData = { org: 'o', row, title: verified, owner: 'Ada', workgroup: null, now: 1 };
    expect(sameTaskItem(item, { ...item, title: { status: 'verified', text: 'A' } })).toBe(true);
    for (const change of [
      { row: { ...row } },
      { org: 'p' },
      { owner: null },
      { workgroup: 'Default' },
      { now: 2 },
      { title: missing },
    ]) {
      expect(sameTaskItem(item, { ...item, ...change })).toBe(false);
    }
  });
});
