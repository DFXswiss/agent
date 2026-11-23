// U-WEB-ui-tasks (screens): composer, countdown panel, task list, home and task page in jsdom against the core test
// world (verified org, keys, platform log, scriptable client). The Playwright specs run the same screens against the
// real hub; these tests cover the states that are slow or impossible to produce there.
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { setOnline } from '../../src/core/store/slices/connection';
import { putEvents } from '../../src/core/store/slices/events';
import { orgEntry, setLog } from '../../src/core/store/slices/orgs';
import { putLaunch, putSession, putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { CHIPS, Composer } from '../../src/ui/composer/Composer';
import { CountdownPanel } from '../../src/ui/countdown/CountdownPanel';
import { I18nProvider } from '../../src/ui/i18n';
import { HomeScreen } from '../../src/ui/screens/home/HomeScreen';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { relativeLabel } from '../../src/ui/screens/tasks/TaskItem';
import { TaskListScreen } from '../../src/ui/screens/tasks/TaskListScreen';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';

afterEach(() => vi.useRealTimers());

function mount(t: TaskWorld, routes: { path: string; element: ReactNode }[], path: string, locale: 'en' | 'de' = 'en') {
  const router = createMemoryRouter([...routes, { path: '*', element: <p data-testid="elsewhere">elsewhere</p> }], {
    initialEntries: [path],
  });
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

function created(t: TaskWorld, body: unknown) {
  const id = (body as { task_id: string }).task_id;
  return {
    task: taskRow(t, { task_id: id }),
    session: { session_id: 's1', state: 'requested', device_id: 'd', estimate_ms: 3000 },
    events: [],
  };
}

describe('composer', () => {
  it('fills from the chips, shows the GitHub hint and sends with Ctrl+Enter to the task page', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }));
    t.client.on('POST /v1/tasks', (body) => created(t, body));
    const router = mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} suggestions /> }], '/');
    expect(CHIPS).toEqual(['research', 'github', 'explain', 'plan']);
    const prompt = screen.getByTestId('composer.prompt');
    fireEvent.click(screen.getByTestId('composer.chip.github'));
    expect(prompt).toHaveValue('Open a pull request that ');
    expect(screen.getByTestId('composer.github-connect')).toHaveAttribute('href', '/settings');
    fireEvent.change(prompt, { target: { value: 'What is 2+2?' } });
    fireEvent.keyDown(prompt, { key: 'Enter' });
    fireEvent.keyDown(prompt, { key: 'a', ctrlKey: true });
    expect(t.client.of('POST /v1/tasks')).toHaveLength(0);
    fireEvent.keyDown(prompt, { key: 'Enter', metaKey: true });
    await waitFor(() => expect(router.state.location.pathname).toMatch(new RegExp(`^/o/${t.org}/t/`)));
    expect(t.client.of('POST /v1/tasks')).toHaveLength(1);
  });

  it('keeps the prepared request after a failure and sends it again unchanged; links the devices screen', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [] }));
    t.client.on(`GET /v1/workgroups/${t.wg}/device-candidates`, () => ({ candidates: [] }));
    const router = mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    expect(screen.queryByTestId('composer.chips')).toBeNull();
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Hello' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    const error = await screen.findByTestId('composer.error');
    expect(error).toHaveAttribute('data-code', 'no_device_available');
    expect(screen.getByTestId('composer.devices')).toHaveAttribute('href', `/o/${t.org}/devices`);
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }));
    let posts = 0;
    t.client.on('POST /v1/tasks', (body) => {
      posts += 1;
      if (posts === 1) throw new HubError('unavailable', 503);
      return created(t, body);
    });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(screen.getByTestId('composer.error')).toHaveAttribute('data-code', 'unavailable'));
    expect(screen.queryByTestId('composer.devices')).toBeNull();
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    const [first, second] = t.client.of('POST /v1/tasks');
    expect(JSON.stringify(first!.body)).toBe(JSON.stringify(second!.body));
  });

  it('prepares a new request when the prompt changed, shows the countdown while sending, says Offline offline', async () => {
    const t = await taskWorld();
    let release: () => void = () => undefined;
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [] }));
    t.client.on(
      `GET /v1/workgroups/${t.wg}/device-candidates`,
      () => new Promise((resolve) => (release = () => resolve({ candidates: [] }))),
    );
    mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'One' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    expect(await screen.findByTestId('countdown.step.assigning')).toHaveAttribute('data-status', 'current');
    expect(screen.getByTestId('countdown.step.requested')).toHaveAttribute('data-status', 'pending');
    expect(screen.getByTestId('composer.send')).toBeDisabled();
    act(() => release());
    await screen.findByTestId('composer.error');
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => new Promise(() => undefined));
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Two' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    expect(await screen.findByTestId('countdown.step.requested')).toHaveAttribute('data-status', 'current');
    expect(screen.queryByTestId('countdown.step.assigning')).toBeNull();
    const prepared = t.client.calls.length;
    expect(prepared).toBeGreaterThan(0);
    act(() => setOnline(t.store, false));
    expect(screen.getByTestId('composer.send')).toHaveTextContent('Offline');
  });

  it('prepares a new request after a refusal a retry cannot fix', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }));
    let posts = 0;
    t.client.on('POST /v1/tasks', (body) => {
      posts += 1;
      if (posts === 1) throw new HubError('epoch_stale', 400);
      return created(t, body);
    });
    const router = mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Hello' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await screen.findByTestId('composer.error');
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    const [first, second] = t.client.of('POST /v1/tasks');
    expect((first!.body as { task_id: string }).task_id).not.toBe((second!.body as { task_id: string }).task_id);
  });

  it('does not send a blank prompt', async () => {
    const t = await taskWorld();
    mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    fireEvent.submit(screen.getByTestId('composer'));
    fireEvent.keyDown(screen.getByTestId('composer.prompt'), { key: 'Enter', ctrlKey: true });
    expect(t.client.calls).toHaveLength(0);
  });
});

describe('countdown panel', () => {
  const T = (t: TaskWorld) => ({ org: t.org, wg: t.wg, me: t.me });

  async function panel(
    setup: (t: TaskWorld, task: TaskRow) => void,
    taskOverrides: Record<string, unknown> = {},
    onRetry?: () => Promise<void>,
    locale: 'en' | 'de' = 'en',
  ) {
    const t = await taskWorld();
    const task = taskRow(T(t), { created_at: new Date().toISOString(), ...taskOverrides }) as TaskRow;
    setup(t, task);
    mount(t, [{ path: '/', element: <CountdownPanel task={task} {...(onRetry && { onRetry })} /> }], '/', locale);
    return t;
  }

  it('counts down with digits and ring, the assignment step and the steps reached', async () => {
    // the clock stands still: the expected digits must not depend on how long the setup takes on a loaded device
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['Date'] });
    await panel(
      (t, task) => {
        putLaunch(t.store, task.task_id, { sessionId: 's9', localAt: Date.now(), estimateMs: 8000, assignedMs: 1234 });
      },
      { main_session_id: null },
    );
    expect(screen.getByTestId('countdown.panel')).toHaveAttribute('data-phase', 'counting');
    expect(screen.getByTestId('countdown.digits')).toHaveTextContent(/^[78]$/);
    expect(screen.getByTestId('countdown.ring')).toBeInTheDocument();
    expect(screen.getByTestId('countdown.step.assigning')).toHaveTextContent('+1.2 s');
    expect(screen.getByTestId('countdown.step.requested')).toHaveAttribute('data-status', 'current');
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('About 10 seconds left: Task accepted');
    expect(document.querySelector('.animate-spin')).toBeNull();
  });

  it('shows "Delayed" with the current step and the elapsed time, in German with a decimal comma', async () => {
    // the clock stands still: the expected digits must not depend on how long the setup takes on a loaded device
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['Date'] });
    await panel(
      (t, task) => {
        putSession(
          t.store,
          sessionRow(T(t), {
            task_id: task.task_id,
            created_at: task.created_at,
            estimate_ms: 0,
            state: 'vm_ready',
          }) as never,
          [{ seq: 1, state: 'vm_ready', reason: null, at: task.created_at, received_at: task.created_at }],
          0,
        );
      },
      { created_at: new Date(Date.now() - 12_400).toISOString() },
      undefined,
      'de',
    );
    expect(screen.getByTestId('countdown.delayed')).toHaveTextContent('Verzögert');
    expect(screen.getByTestId('countdown.elapsed')).toHaveTextContent(/^Sandbox bereit – 12,\d s$/);
    expect(screen.getByTestId('countdown.step.vm-ready').querySelector('time')).toHaveTextContent(
      /^\d\d:\d\d:\d\d[.,]\d{3}$/,
    );
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('Verzögert, 10 Sekunden: Sandbox bereit');
  });

  it('names the reason of a waiting task, a generic text without one', async () => {
    await panel(
      (t, task) =>
        putSession(
          t.store,
          sessionRow(T(t), {
            task_id: task.task_id,
            created_at: task.created_at,
            estimate_ms: null,
            device_id: null,
          }) as never,
          [],
          0,
        ),
      { state: 'scheduling', state_reason: 'capacity_exhausted' },
    );
    expect(screen.getByTestId('countdown.reason')).toHaveAttribute('data-reason', 'capacity_exhausted');
    expect(screen.getByTestId('countdown.elapsed')).toHaveTextContent(/^Waiting for \d\.\d s$/);
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('Waiting for 0 seconds');
    document.body.innerHTML = '';
    await panel(() => undefined, { main_session_id: null });
    expect(screen.getByTestId('countdown.reason')).toHaveAttribute('data-reason', 'unknown');
    expect(screen.getByTestId('countdown.reason')).toHaveTextContent('Waiting for an execution device.');
  });

  it('collapses into "Ready in" once working is reached', async () => {
    await panel((t, task) => {
      const at = new Date(Date.parse(task.created_at) + 4200).toISOString();
      putSession(
        t.store,
        sessionRow(T(t), {
          task_id: task.task_id,
          created_at: task.created_at,
          state: 'working',
          first_working_at: at,
        }) as never,
        [],
        0,
      );
    });
    expect(screen.getByTestId('countdown.panel')).toHaveAttribute('data-phase', 'ready');
    expect(screen.getByTestId('countdown.summary')).toHaveTextContent('Ready in 4.2 s');
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('Ready in 4.2 s');
  });

  it('shows the catalog error of a failed start; Retry reports its own failure', async () => {
    let attempts = 0;
    const retry = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) throw new HubError('rate_limited', 429);
      if (attempts === 2) throw new Error('plain');
    });
    await panel(
      (t, task) =>
        putSession(
          t.store,
          sessionRow(T(t), {
            task_id: task.task_id,
            created_at: task.created_at,
            state: 'failed',
            state_reason: 'oom',
          }) as never,
          [],
          0,
        ),
      {},
      retry,
    );
    expect(screen.getByTestId('countdown.failed')).toHaveAttribute('data-reason', 'oom');
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('The session could not start.');
    fireEvent.click(screen.getByTestId('countdown.retry'));
    expect(await screen.findByTestId('countdown.retry-error')).toHaveTextContent(
      createI18n('en').error('rate_limited').title,
    );
    fireEvent.click(screen.getByTestId('countdown.retry'));
    await waitFor(() =>
      expect(screen.getByTestId('countdown.retry-error')).toHaveTextContent(
        createI18n('en').error('unavailable').title,
      ),
    );
    fireEvent.click(screen.getByTestId('countdown.retry'));
    await waitFor(() => expect(screen.queryByTestId('countdown.retry-error')).toBeNull());
    expect(retry).toHaveBeenCalledTimes(3);
  });

  it('offers no Retry without a handler and falls back to a generic error without a reason', async () => {
    await panel((t, task) =>
      putSession(
        t.store,
        sessionRow(T(t), { task_id: task.task_id, created_at: task.created_at, state: 'failed' }) as never,
        [],
        0,
      ),
    );
    expect(screen.getByTestId('countdown.failed')).toHaveAttribute('data-reason', '');
    expect(screen.getByTestId('countdown.failed')).toHaveTextContent(createI18n('en').error('unavailable').title);
    expect(screen.queryByTestId('countdown.retry')).toBeNull();
  });

  it('follows the clock while counting', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['setInterval', 'clearInterval', 'Date'] });
    await panel(
      (t, task) =>
        putLaunch(t.store, task.task_id, { sessionId: 'x', localAt: Date.now(), estimateMs: 5000, assignedMs: null }),
      {
        main_session_id: null,
      },
    );
    expect(screen.getByTestId('countdown.digits')).toHaveTextContent('5');
    act(() => void vi.advanceTimersByTime(2600));
    expect(screen.getByTestId('countdown.digits')).toHaveTextContent('2.4');
  });
});

describe('task list and home', () => {
  const rows = (t: TaskWorld, n: number, extra: Record<string, unknown> = {}) =>
    Array.from({ length: n }, (_, i) =>
      taskRow(t, { task_id: `0190f000-0000-7000-8000-0000000001${String(i).padStart(2, '0')}`, ...extra }),
    );

  it('shows rows with titles, owner and relative activity, pages with "Load more", reloads after an error', async () => {
    const t = await taskWorld();
    const base = `GET /v1/workgroups/${t.wg}/tasks`;
    let fail = true;
    t.client.on(base, (_b, target) => {
      if (fail) throw new HubError('unavailable', 503);
      return target.includes('before=')
        ? {
            tasks: rows(t, 1, {
              task_id: 'z',
              state: 'mystery',
              owner_id: 'stranger',
              created_at: '2026-11-20T12:00:00.000Z',
            }),
            next_before: null,
          }
        : { tasks: rows(t, 2, { state: 'working', owner_id: t.me.id }), next_before: 'c' };
    });
    t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    putEvents(t.store, t.org, [
      {
        org: t.org,
        scopeId: t.org,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'member.profile', ref: t.me.id, author: t.me.id, id: 'm1' },
        body: { display_name: 'Ada' },
      },
      {
        org: t.org,
        scopeId: t.org,
        scopeSeq: 2,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'wg.profile', wg: t.wg, author: t.me.id, id: 'w1' },
        body: { name: 'Default' },
      },
    ]);
    putEvents(t.store, t.wg, [
      {
        org: t.org,
        scopeId: t.wg,
        scopeSeq: 1,
        receivedAt: null,
        event: { header: { id: 'bad' } },
        status: 'unverifiable',
        code: 'signature_invalid',
        header: null,
        body: null,
      },
      {
        // the signed author of the second task's task.meta is the owner, whatever the hub's owner_id says
        org: t.org,
        scopeId: t.wg,
        scopeSeq: 2,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: {
          kind: 'task.meta',
          id: 'meta-2',
          task: '0190f000-0000-7000-8000-000000000101',
          author: t.me.id,
          ts: '2026-11-21T12:00:00.000Z',
        },
        body: { title: 'Second' },
      },
    ]);
    mount(t, [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }], `/o/${t.org}/w/${t.wg}`);
    expect(await screen.findByTestId('tasks.error')).toHaveTextContent(createI18n('en').error('unavailable').title);
    fail = false;
    fireEvent.click(screen.getByText('Try again'));
    await waitFor(() => expect(screen.getAllByTestId('tasks.row')).toHaveLength(2));
    expect(screen.getByTestId('tasks.title')).toHaveTextContent('Default');
    // the hub names Ada as owner of both: only the task with her signed task.meta shows her, the other has none yet
    expect(screen.getAllByTestId('tasks.row.owner')[0]).toHaveTextContent('Ada');
    expect(screen.getAllByTestId('tasks.row.owner')[1]).toHaveTextContent('Member');
    expect(screen.getAllByTestId('tasks.row.title')[1]).toHaveAttribute('data-status', 'missing');
    fireEvent.click(screen.getByTestId('tasks.more'));
    await waitFor(() => expect(screen.getAllByTestId('tasks.row')).toHaveLength(3));
    const last = screen.getAllByTestId('tasks.row')[2]!;
    expect(last.querySelector('[data-testid="tasks.row.state"]')).toHaveTextContent('tasks.state.mystery');
    expect(last.querySelector('[data-testid="tasks.row.owner"]')).toHaveTextContent('Member');
    expect(screen.queryByTestId('tasks.more')).toBeNull();
    act(() =>
      putTasks(t.store, [
        taskRow(t, { task_id: 'u', title_event_id: 'bad', created_at: '2026-11-22T00:00:00.000Z' }) as TaskRow,
      ]),
    );
    expect(screen.getAllByTestId('tasks.row.title')[0]).toHaveTextContent('Unverifiable title (signature_invalid)');
    fireEvent.change(screen.getByTestId('tasks.filter'), { target: { value: 'failed' } });
    await waitFor(() => expect(t.client.calls.some((call) => call.target.includes('state=failed'))).toBe(true));
  });

  it('formats relative activity labels', () => {
    expect(relativeLabel('en', 3 * 86_400_000)).toBe('3 days ago');
    expect(relativeLabel('en', 2 * 3_600_000)).toBe('2 hr. ago');
    expect(relativeLabel('en', 5 * 60_000)).toBe('5 min. ago');
    expect(relativeLabel('en', 12_000)).toBe('12 sec. ago');
    expect(relativeLabel('en', -5)).toBe('now');
  });

  it('waits for the verified log and hides unknown or unreadable workgroups', async () => {
    const t = await taskWorld();
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, log: null } },
    }));
    mount(t, [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }], `/o/${t.org}/w/${t.wg}`);
    expect(screen.getByTestId('tasks.loading')).toBeInTheDocument();
    act(() => setLog(t.store, t.org, t.world.log!));
    expect(screen.getByTestId('tasks.screen')).toBeInTheDocument();
    act(() => t.store.update('me', (me) => ({ ...me, userId: null })));
    expect(screen.getByTestId('tasks.not-found')).toBeInTheDocument();
  });

  it('home: loading, unnamed org, the workgroup choice and the read-only case', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => ({ tasks: [], next_before: null }));
    const log = orgEntry(t.store.getState().orgs, t.org).log!;
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, log: null } },
    }));
    mount(t, [{ path: '/o/:org', element: <HomeScreen /> }], `/o/${t.org}`);
    expect(screen.getByTestId('home.loading')).toBeInTheDocument();
    await t.world.batch(t.ada, [{ type: 'wg_create', wg: '0190f000-0000-7000-8000-00000000000a', epoch: 1 }]);
    act(() => setLog(t.store, t.org, t.world.log!));
    expect(screen.getByTestId('home.org')).toHaveTextContent('Organization');
    const select = screen.getByTestId('home.wg');
    expect([...select.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'Default workgroup',
      'Workgroup',
    ]);
    fireEvent.change(select, { target: { value: '0190f000-0000-7000-8000-00000000000a' } });
    expect(select).toHaveValue('0190f000-0000-7000-8000-00000000000a');
    expect(screen.getByTestId('home.all-tasks')).toHaveAttribute('href', `/o/${t.org}/w/${t.wg}`);
    act(() => setLog(t.store, t.org, log));
    expect(select).toHaveValue(t.wg);
    act(() => t.store.update('me', (me) => ({ ...me, userId: 'someone-else' })));
    expect(screen.getByTestId('home.read-only')).toBeInTheDocument();
    expect(screen.queryByTestId('composer')).toBeNull();
    expect(screen.queryByTestId('home.wg')).toBeNull();
  });
});

describe('task page', () => {
  it('loads the task, shows title, prompt and countdown, and retries a failed start', async () => {
    const t = await taskWorld();
    const row = taskRow(t, { state: 'failed', state_reason: 'oom' });
    const session = sessionRow(t, { state: 'failed', state_reason: 'oom' });
    t.client.on(`GET /v1/tasks/${row.task_id}`, () => ({
      task: row,
      sessions: [{ session, usage: null, transitions: [] }],
      transitions: [],
    }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    t.client.on(`POST /v1/tasks/${row.task_id}/prompts`, () => ({ scope_seq: 3, session_id: 's2' }));
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${row.task_id}`);
    expect(screen.getByTestId('task.loading')).toHaveTextContent('Loading the task…');
    expect(await screen.findByTestId('task.title')).toHaveTextContent('Loading title …');
    expect(screen.queryByTestId('chat.prompt')).toBeNull();
    act(() =>
      putEvents(t.store, t.wg, [
        {
          org: t.org,
          scopeId: t.wg,
          scopeSeq: 1,
          receivedAt: null,
          event: {},
          status: 'verified',
          code: null,
          header: { kind: 'task.meta', task: row.task_id, id: 'm' },
          body: { title: 'Hello' },
        },
        {
          org: t.org,
          scopeId: t.wg,
          scopeSeq: 2,
          receivedAt: null,
          event: {},
          status: 'verified',
          code: null,
          header: { kind: 'prompt', task: row.task_id, id: 'p' },
          body: { text: 'Hello there', attachments: [] },
        },
        {
          org: t.org,
          scopeId: t.wg,
          scopeSeq: 3,
          receivedAt: null,
          event: {},
          status: 'verified',
          code: null,
          header: { kind: 'prompt', task: row.task_id, id: 'p2' },
          body: { text: 3 },
        },
      ]),
    );
    expect(screen.getByTestId('task.title')).toHaveTextContent('Hello');
    expect(screen.getAllByTestId('chat.prompt.text')[0]).toHaveTextContent('Hello there');
    fireEvent.click(screen.getByTestId('countdown.retry'));
    await waitFor(() => expect(t.client.of(`POST /v1/tasks/${row.task_id}/prompts`)).toHaveLength(1));
  });

  it('offers Retry only to users who may write in the workgroup', async () => {
    const t = await taskWorld();
    const row = taskRow(t, { state: 'failed', state_reason: 'oom' });
    putTasks(t.store, [row as TaskRow]);
    putSession(t.store, sessionRow(t, { state: 'failed', state_reason: 'oom' }) as never, [], 0);
    t.store.update('me', (me) => ({ ...me, userId: 'reader' }));
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${row.task_id}`);
    expect(screen.getByTestId('countdown.failed')).toBeInTheDocument();
    expect(screen.queryByTestId('countdown.retry')).toBeNull();
  });

  it('shows the catalog error of a task that cannot be loaded', async () => {
    const t = await taskWorld();
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/x`);
    expect(await screen.findByTestId('task.error')).toHaveTextContent('Not available');
  });

  it('shows a prompt that is not text as an empty bubble', async () => {
    const t = await taskWorld();
    const row = taskRow(t);
    putTasks(t.store, [row as TaskRow]);
    putEvents(t.store, t.wg, [
      {
        org: t.org,
        scopeId: t.wg,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'prompt', task: row.task_id, id: 'p' },
        body: { text: 3 },
      },
    ]);
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${row.task_id}`);
    expect(screen.getByTestId('task.screen')).toBeInTheDocument();
    expect(screen.getByTestId('chat.prompt.text')).toHaveTextContent('');
  });
});
