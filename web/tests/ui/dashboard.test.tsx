// U-WEB-ui-tasks (dashboard, ADR-101 section 3): the task overview as the dashboard of every organization of
// the user – rows of both organizations with "organization · workgroup", the organization filter, the counters per
// status and urgency summed over the organizations (a click filters, a second click clears), the order, the counters
// loaded again after a changed row or a reconnect, and a failed counter request. The Playwright spec runs the same
// screen against the hub.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { ensureOrg, setLog } from '../../src/core/store/slices/orgs';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { I18nProvider } from '../../src/ui/i18n';
import { OverviewScreen } from '../../src/ui/screens/tasks/OverviewScreen';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { idOf, World } from '../core/log/world';
import { taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';

const at = (minute: number) => `2026-11-21T12:${String(minute).padStart(2, '0')}:00.000Z`;

function event(org: string, scope: string, seq: number, header: Record<string, unknown>, body: unknown): EventEntry {
  return {
    org,
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

const counts = (todo: number, progress: number, urgent: number) => ({
  by_status: { todo, inProgress: progress, completed: 0 },
  by_urgency: { low: 0, normal: todo + progress - urgent, high: 0, urgent },
  total: todo + progress,
});

/**
 * Ada in two organizations: "Acme" (the route's, a captured urgent task and an agent task) and "Beta" (one task;
 * `named: false` leaves out its profiles).
 */
async function world({ named = true }: { named?: boolean } = {}) {
  const t = await taskWorld();
  // a second organization with its own IDs (`World.create` names every org alike)
  const beta = new World(await idOf('beta org'), await idOf('beta wg'));
  beta.add(t.me);
  await beta.batch(t.me, [
    { type: 'genesis', subject: t.me.id, pub: t.me.identity.public(), default_wg: beta.defaultWg, private: false },
  ]);
  ensureOrg(t.store, beta.org);
  setLog(t.store, beta.org, beta.log!);
  t.store.update('me', (me) => ({
    ...me,
    profile: {
      orgs: [
        { org_id: t.org, role: 'admin', private: true, default_wg: t.wg },
        { org_id: beta.org, role: 'admin', private: false, default_wg: beta.defaultWg },
        { org_id: 'broken' },
      ],
    },
  }));
  putEvents(t.store, t.org, [
    event(t.org, t.org, 1, { kind: 'org.profile', author: t.me.id }, { name: 'Acme' }),
    event(t.org, t.org, 2, { kind: 'wg.profile', wg: t.wg, author: t.me.id }, { name: 'Default' }),
  ]);
  putEvents(t.store, beta.org, [
    ...(named ? [event(beta.org, beta.org, 1, { kind: 'org.profile', author: t.me.id }, { name: 'Beta' })] : []),
    event(beta.org, beta.org, 2, { kind: 'wg.profile', wg: beta.defaultWg, author: t.me.id }, { name: 'Ops' }),
  ]);
  const titles = (org: string, wg: string, items: [string, string][]) =>
    putEvents(
      t.store,
      wg,
      items.map(([task, title], index) =>
        event(org, wg, index + 1, { kind: 'task.meta', task, author: t.me.id }, { title }),
      ),
    );
  titles(t.org, t.wg, [
    ['a1', 'Fix payments'],
    ['a2', 'Weekly report'],
  ]);
  titles(beta.org, beta.defaultWg, [['b1', 'Write docs']]);
  const rows: Record<string, TaskRow[]> = {
    [t.wg]: [
      taskRow(t, { task_id: 'a2', last_activity_at: at(5), status: 'inProgress', urgency: 'normal' }) as TaskRow,
      taskRow(t, {
        task_id: 'a1',
        last_activity_at: at(1),
        state: 'idle',
        status: 'todo',
        urgency: 'urgent',
      }) as TaskRow,
    ],
    [beta.defaultWg]: [
      taskRow(t, {
        task_id: 'b1',
        org_id: beta.org,
        wg_id: beta.defaultWg,
        last_activity_at: at(3),
        state: 'idle',
        status: 'todo',
      }) as TaskRow,
    ],
  };
  for (const [wg, list] of Object.entries(rows)) {
    t.client.on(`GET /v1/workgroups/${wg}/tasks`, (_body, target) => {
      const params = new URLSearchParams(target.split('?')[1]);
      const status = params.get('status');
      const found = list.filter((row) => status === null || row.status === status);
      const ranked =
        params.get('order') === 'urgency'
          ? [...found].sort((a, b) => Number(b.urgency === 'urgent') - Number(a.urgency === 'urgent'))
          : found;
      return { tasks: ranked, next_before: null };
    });
  }
  t.client.on(`GET /v1/orgs/${t.org}/tasks/counts`, (_body, target) =>
    target.includes('status=todo') ? counts(1, 0, 1) : counts(1, 1, 1),
  );
  t.client.on(`GET /v1/orgs/${beta.org}/tasks/counts`, () => counts(1, 0, 0));
  for (const scope of [t.org, t.wg, beta.org, beta.defaultWg]) {
    t.client.on(`GET /v1/scopes/${scope}/events`, () => ({ events: [], next_after: null, head: 0 }));
  }
  return { t, beta, rows };
}

function mount(t: TaskWorld) {
  const router = createMemoryRouter([{ path: '/o/:org/tasks', element: <OverviewScreen /> }], {
    initialEntries: [`/o/${t.org}/tasks`],
  });
  const app = { store: t.store, tasks: t.context } as never;
  render(
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n('en')}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
}

const titles = () => screen.queryAllByTestId('tasks.row.title').map((node) => node.textContent);
const count = (id: string) => screen.getByTestId(`dashboard.count.${id}`).getAttribute('data-count');
const countCalls = (t: TaskWorld) => t.client.calls.filter((call) => call.target.includes('/tasks/counts'));

describe('task dashboard', () => {
  it('lists the tasks of every organization with their place and sums the counters', async () => {
    const { t, beta } = await world();
    mount(t);
    await waitFor(() => expect(titles()).toEqual(['Weekly report', 'Write docs', 'Fix payments']));
    expect(screen.getAllByTestId('tasks.row.wg').map((node) => node.textContent)).toEqual([
      'Acme · Default',
      'Beta · Ops',
      'Acme · Default',
    ]);
    await waitFor(() => expect(count('status.todo')).toBe('2'));
    expect(count('status.inProgress')).toBe('1');
    expect(count('status.completed')).toBe('0');
    expect(count('urgency.urgent')).toBe('1');
    expect(screen.getByTestId('dashboard.counts.total')).toHaveTextContent('Tasks: 3');
    const orgs = within(screen.getByTestId('tasks.filter.org')).getAllByRole('option');
    expect(orgs.map((node) => node.textContent)).toEqual(['All organizations', 'Acme', 'Beta']);
    expect(
      within(screen.getByTestId('tasks.filter.wg'))
        .getAllByRole('option')
        .map((node) => node.textContent),
    ).toEqual(['All workgroups', 'Acme · Default', 'Beta · Ops']);
    // one organization: its rows, its workgroups, its counters
    fireEvent.change(screen.getByTestId('tasks.filter.org'), { target: { value: beta.org } });
    await waitFor(() => expect(titles()).toEqual(['Write docs']));
    expect(screen.getByTestId('tasks.row.wg')).toHaveTextContent('Ops');
    await waitFor(() => expect(count('status.inProgress')).toBe('0'));
    // a workgroup of another organization is left when the organization changes; the reset shows all again
    fireEvent.change(screen.getByTestId('tasks.filter.wg'), { target: { value: beta.defaultWg } });
    expect(countCalls(t).some((call) => call.target === `/v1/orgs/${beta.org}/tasks/counts?wg=${beta.defaultWg}`)).toBe(
      true,
    );
    fireEvent.change(screen.getByTestId('tasks.filter.org'), { target: { value: t.org } });
    await waitFor(() => expect(titles()).toEqual(['Weekly report', 'Fix payments']));
    expect(screen.getByTestId('tasks.filter.wg')).toHaveValue('');
    fireEvent.change(screen.getByTestId('tasks.search'), { target: { value: 'nothing' } });
    fireEvent.click(await screen.findByTestId('tasks.filter.reset'));
    await waitFor(() => expect(titles()).toHaveLength(3));
    expect(screen.getByTestId('tasks.filter.org')).toHaveValue('');
  });

  it('filters by a click on a counter, clears with a second click, and sorts', async () => {
    const { t } = await world();
    mount(t);
    await waitFor(() => expect(titles()).toHaveLength(3));
    const todo = screen.getByTestId('dashboard.count.status.todo');
    expect(todo).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(todo);
    await waitFor(() => expect(titles()).toEqual(['Write docs', 'Fix payments']));
    expect(todo).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('tickets.filter.status')).toHaveValue('todo');
    // the status counters ignore the status filter (the hub's facets): the other counts stay visible
    expect(countCalls(t).some((call) => call.target.endsWith('counts?status=todo'))).toBe(true);
    fireEvent.click(todo);
    await waitFor(() => expect(titles()).toHaveLength(3));
    expect(screen.getByTestId('tickets.filter.status')).toHaveValue('');
    fireEvent.click(screen.getByTestId('dashboard.count.urgency.urgent'));
    expect(screen.getByTestId('tickets.filter.urgency')).toHaveValue('urgent');
    fireEvent.click(screen.getByTestId('dashboard.count.urgency.urgent'));
    expect(screen.getByTestId('tickets.filter.urgency')).toHaveValue('');
    fireEvent.change(screen.getByTestId('tasks.order'), { target: { value: 'urgency' } });
    await waitFor(() => expect(titles()).toEqual(['Fix payments', 'Weekly report', 'Write docs']));
    expect(
      t.client.calls.some((call) => call.target.includes(`${t.wg}/tasks?`) && call.target.includes('order=urgency')),
    ).toBe(true);
  });

  it('loads the counters again after a changed row and a reconnect, and says when they fail', async () => {
    const { t, rows } = await world();
    mount(t);
    await waitFor(() => expect(count('status.todo')).toBe('2'));
    const before = countCalls(t).length;
    act(() => putTasks(t.store, [{ ...rows[t.wg]![1]!, status: 'inProgress', last_activity_at: at(9) } as TaskRow]));
    await waitFor(() => expect(countCalls(t).length).toBeGreaterThan(before));
    t.client.on(`GET /v1/orgs/${t.org}/tasks/counts`, () => {
      throw new HubError('unavailable', 503);
    });
    act(() => t.store.update('connection', (connection) => ({ ...connection, round: connection.round + 1 })));
    expect(await screen.findByTestId('dashboard.counts.error')).toHaveTextContent('The counts could not be loaded.');
    // the last counts stay
    expect(count('status.todo')).toBe('2');
  });

  it('shows dashes until the first counters arrive, and an organization without a read name generically', async () => {
    const { t } = await world({ named: false });
    t.client.on(`GET /v1/orgs/${t.org}/tasks/counts`, () => new Promise(() => undefined));
    mount(t);
    await waitFor(() => expect(titles()).toHaveLength(3));
    expect(screen.getAllByTestId('tasks.row.wg')[1]).toHaveTextContent('Organization · Ops');
    expect(
      within(screen.getByTestId('tasks.filter.org'))
        .getAllByRole('option')
        .map((node) => node.textContent),
    ).toEqual(['All organizations', 'Acme', 'Organization']);
    expect(screen.getByTestId('dashboard.count.status.todo')).toHaveTextContent('–');
    expect(screen.getByTestId('dashboard.counts')).toHaveAttribute('aria-busy', 'true');
  });
});
