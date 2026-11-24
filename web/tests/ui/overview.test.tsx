// U-WEB-ui-tasks (overview, KOR-32): the task overview of an organization in jsdom against the core test world – the
// readable workgroups only (an unreadable one is never requested nor offered), the merged rows with their workgroup,
// the filters (state, workgroup, only mine, title search), the empty states, errors with retry, "Load more", and the
// fallback when the chosen workgroup is no longer readable. The Playwright spec runs the same screen against the hub.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { setLog } from '../../src/core/store/slices/orgs';
import { I18nProvider } from '../../src/ui/i18n';
import { OverviewScreen } from '../../src/ui/screens/tasks/OverviewScreen';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { idOf } from '../core/log/world';
import { taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';

function mount(t: TaskWorld, locale: 'en' | 'de' = 'en') {
  const router = createMemoryRouter(
    [
      { path: '/o/:org/tasks', element: <OverviewScreen /> },
      { path: '*', element: <p data-testid="elsewhere">elsewhere</p> },
    ],
    { initialEntries: [`/o/${t.org}/tasks`] },
  );
  const app = { store: t.store, tasks: t.context } as never;
  render(
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n(locale)}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
  return router;
}

function event(t: TaskWorld, scope: string, seq: number, header: Record<string, unknown>, body: unknown): EventEntry {
  return {
    org: t.org,
    scopeId: scope,
    scopeSeq: seq,
    receivedAt: null,
    event: {},
    status: 'verified',
    code: null,
    header: { id: `${scope}-${seq}`, ts: '2026-11-21T12:00:00.000Z', ...header },
    body,
  };
}

const at = (minute: number) => `2026-11-21T12:${String(minute).padStart(2, '0')}:00.000Z`;

/** A member (Bob) of Ada's org: the default workgroup, "Research" (read), "Secret" (no access), "Old" (archived). */
async function world() {
  const t = await taskWorld({ member: true });
  const [research, secret, old] = [await idOf('research'), await idOf('secret'), await idOf('old')];
  const before = t.world.log!;
  await t.world.batch(t.ada, [
    { type: 'wg_create', wg: research, epoch: 1 },
    { type: 'grant', subject: t.me.id, wg: research, access: 'read' },
    { type: 'wg_create', wg: secret, epoch: 1 },
    { type: 'wg_create', wg: old, epoch: 1 },
    { type: 'grant', subject: t.me.id, wg: old, access: 'read' },
  ]);
  await t.world.batch(t.ada, [{ type: 'wg_archive', wg: old }]);
  setLog(t.store, t.org, t.world.log!);
  putEvents(t.store, t.org, [
    event(t, t.org, 1, { kind: 'wg.profile', wg: t.wg, author: t.ada.id }, { name: 'Default' }),
    event(t, t.org, 2, { kind: 'wg.profile', wg: research, author: t.ada.id }, { name: 'Research' }),
    event(t, t.org, 3, { kind: 'wg.profile', wg: secret, author: t.ada.id }, { name: 'Secret' }),
    event(t, t.org, 4, { kind: 'member.profile', ref: t.me.id, author: t.me.id }, { display_name: 'Bob' }),
    event(t, t.org, 5, { kind: 'member.profile', ref: t.ada.id, author: t.ada.id }, { display_name: 'Ada' }),
  ]);
  const titles = (wg: string, items: [string, string, string][]) =>
    putEvents(
      t.store,
      wg,
      items.map(([task, author, title], index) =>
        event(t, wg, index + 1, { kind: 'task.meta', task, author }, { title }),
      ),
    );
  titles(t.wg, [
    ['d1', t.me.id, 'Weekly report'],
    ['d2', t.ada.id, 'Invoice check'],
  ]);
  titles(research, [['r1', t.ada.id, 'Market report']]);
  const pages: Record<string, unknown[]> = {
    [t.wg]: [
      taskRow(t, { task_id: 'd1', last_activity_at: at(5), state: 'working' }),
      taskRow(t, { task_id: 'd2', last_activity_at: at(1) }),
    ],
    [research]: [taskRow(t, { task_id: 'r1', wg_id: research, last_activity_at: at(3) })],
    [old]: [],
  };
  for (const [wg, rows] of Object.entries(pages)) {
    t.client.on(`GET /v1/workgroups/${wg}/tasks`, () => ({ tasks: rows, next_before: null }));
  }
  for (const scope of [t.org, t.wg, research, old]) {
    t.client.on(`GET /v1/scopes/${scope}/events`, () => ({ events: [], next_after: null, head: 0 }));
  }
  return { t, research, secret, old, before };
}

const titles = () => screen.queryAllByTestId('tasks.row.title').map((node) => node.textContent);
const listed = (t: TaskWorld) => new Set(t.client.calls.map((call) => call.target.split('?')[0]!));

describe('task overview', () => {
  it('merges the readable workgroups newest activity first and never requests the others', async () => {
    const { t, research, secret, old } = await world();
    mount(t);
    await waitFor(() => expect(titles()).toEqual(['Weekly report', 'Market report', 'Invoice check']));
    expect(screen.getByTestId('tasks.overview.title')).toHaveTextContent('Tasks');
    expect(screen.getAllByTestId('tasks.row.wg').map((node) => node.textContent)).toEqual([
      'Default',
      'Research',
      'Default',
    ]);
    expect(screen.getAllByTestId('tasks.row.owner').map((node) => node.textContent)).toEqual(['Bob', 'Ada', 'Ada']);
    const requested = listed(t);
    for (const wg of [t.wg, research, old]) expect(requested).toContain(`/v1/workgroups/${wg}/tasks`);
    expect([...requested].some((target) => target.includes(secret))).toBe(false);
    const options = within(screen.getByTestId('tasks.filter.wg')).getAllByRole('option');
    expect(options.map((node) => node.textContent)).toEqual([
      'All workgroups',
      'Default',
      'Research',
      'Workgroup (archived)',
    ]);
  });

  it('filters by workgroup, owner, title and state, and clears the filters', async () => {
    const { t, research } = await world();
    mount(t);
    await waitFor(() => expect(titles()).toHaveLength(3));
    fireEvent.change(screen.getByTestId('tasks.search'), { target: { value: 'REPORT' } });
    expect(titles()).toEqual(['Weekly report', 'Market report']);
    fireEvent.click(screen.getByTestId('tasks.filter.mine'));
    expect(titles()).toEqual(['Weekly report']);
    fireEvent.change(screen.getByTestId('tasks.filter.wg'), { target: { value: research } });
    expect(screen.getByTestId('tasks.empty')).toHaveTextContent('No task matches the filters.');
    fireEvent.click(screen.getByTestId('tasks.filter.reset'));
    expect(screen.getByTestId('tasks.filter.wg')).toHaveValue('');
    expect(screen.getByTestId('tasks.search')).toHaveValue('');
    expect(screen.getByTestId('tasks.filter.mine')).not.toBeChecked();
    await waitFor(() => expect(titles()).toHaveLength(3));
    fireEvent.change(screen.getByTestId('tasks.filter.wg'), { target: { value: research } });
    await waitFor(() => expect(titles()).toEqual(['Market report']));
    fireEvent.change(screen.getByTestId('tasks.filter.wg'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('tasks.filter'), { target: { value: 'working' } });
    await waitFor(() =>
      expect(t.client.calls.some((call) => call.target.includes(`${t.wg}/tasks?state=working`))).toBe(true),
    );
  });

  it('falls back to all workgroups when the chosen one is no longer readable', async () => {
    const { t, research, before } = await world();
    mount(t);
    await waitFor(() => expect(titles()).toHaveLength(3));
    fireEvent.change(screen.getByTestId('tasks.filter.wg'), { target: { value: research } });
    await waitFor(() => expect(titles()).toEqual(['Market report']));
    act(() => setLog(t.store, t.org, before));
    await waitFor(() => expect(titles()).toEqual(['Weekly report', 'Invoice check']));
    expect(screen.getByTestId('tasks.filter.wg')).toHaveValue('');
  });

  it('explains how to create a task when there is none, in German too', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => ({ tasks: [], next_before: null }));
    const router = mount(t, 'de');
    const empty = await screen.findByTestId('tasks.empty');
    expect(empty).toHaveTextContent('Es gibt noch keine Tasks.');
    expect(empty).toHaveTextContent('Öffne „Start“');
    expect(screen.getAllByTestId('tasks.filter.wg')[0]!.querySelectorAll('option')[1]).toHaveTextContent(
      'Arbeitsgruppe',
    );
    fireEvent.click(screen.getByTestId('tasks.overview.create'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${t.org}`));
  });

  it('waits for the verified log and the user, reports a failed list with a retry and loads more', async () => {
    const t = await taskWorld();
    const log = t.world.log!;
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, log: null } },
    }));
    let fail = true;
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) => {
      if (fail) throw new HubError('unavailable', 503);
      return target.includes('before=')
        ? { tasks: [taskRow(t, { task_id: 'old', last_activity_at: at(0) })], next_before: null }
        : { tasks: [taskRow(t, { task_id: 'new', last_activity_at: at(9) })], next_before: 'next' };
    });
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
    mount(t);
    expect(screen.getByTestId('tasks.loading')).toBeInTheDocument();
    act(() => t.store.update('me', (me) => ({ ...me, userId: null })));
    act(() => setLog(t.store, t.org, log));
    expect(screen.getByTestId('tasks.loading')).toBeInTheDocument();
    act(() => t.store.update('me', (me) => ({ ...me, userId: t.me.id })));
    expect(await screen.findByTestId('tasks.error')).toHaveTextContent(createI18n('en').error('unavailable').title);
    expect(screen.queryByTestId('tasks.empty')).toBeNull();
    fail = false;
    fireEvent.click(screen.getByText('Try again'));
    await waitFor(() => expect(screen.getAllByTestId('tasks.row')).toHaveLength(1));
    expect(screen.getByTestId('tasks.row.wg')).toHaveTextContent('Workgroup');
    fireEvent.click(screen.getByTestId('tasks.more'));
    await waitFor(() => expect(screen.getAllByTestId('tasks.row')).toHaveLength(2));
    expect(screen.queryByTestId('tasks.more')).toBeNull();
  });

  it('loads older pages on its own (bounded) while the title search or "only mine" finds too few rows', async () => {
    const t = await taskWorld();
    const pages: string[] = [];
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) => {
      pages.push(target);
      return target.includes('before=')
        ? { tasks: [taskRow(t, { task_id: 'old', last_activity_at: at(0) })], next_before: null }
        : { tasks: [taskRow(t, { task_id: 'new', last_activity_at: at(9) })], next_before: 'next' };
    });
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
    putEvents(t.store, t.wg, [
      event(t, t.wg, 1, { kind: 'task.meta', task: 'new', author: t.me.id }, { title: 'Weekly report' }),
      event(t, t.wg, 2, { kind: 'task.meta', task: 'old', author: t.me.id }, { title: 'Hidden invoice' }),
    ]);
    mount(t);
    await waitFor(() => expect(titles()).toEqual(['Weekly report']));
    expect(pages).toHaveLength(1);
    fireEvent.change(screen.getByTestId('tasks.search'), { target: { value: 'invoice' } });
    // no "Load more" click: the match on the next page appears, and the end of the lists ends the loading
    await waitFor(() => expect(titles()).toEqual(['Hidden invoice']));
    expect(pages).toHaveLength(2);
    expect(screen.queryByTestId('tasks.more')).toBeNull();
    fireEvent.change(screen.getByTestId('tasks.search'), { target: { value: 'nothing' } });
    expect(await screen.findByTestId('tasks.empty')).toHaveTextContent('No task matches the filters.');
    expect(screen.queryByTestId('tasks.list-loading')).toBeNull();
  });

  it('shows "Loading…" until every first page answered', async () => {
    const t = await taskWorld();
    let answer: (value: unknown) => void = () => undefined;
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => new Promise((resolve) => (answer = resolve)));
    mount(t);
    expect(await screen.findByTestId('tasks.list-loading')).toBeInTheDocument();
    expect(screen.getByTestId('tasks.table')).toHaveAttribute('aria-busy', 'true');
    await act(async () => answer({ tasks: [], next_before: null }));
    await waitFor(() => expect(screen.queryByTestId('tasks.list-loading')).toBeNull());
  });
});
